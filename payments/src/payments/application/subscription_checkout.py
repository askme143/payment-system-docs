from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from payments.application.billing_cycles import next_billing_at
from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentRequiredResponseError,
    ProviderError,
    ResourceNotFoundError,
)
from payments.application.operation_locks import (
    acquire_required_operation_lock,
    release_operation_lock,
)
from payments.application.ports.billing_auth import BillingAuthRepository
from payments.application.ports.billing_keys import BillingKeyCipher
from payments.application.ports.catalog import CatalogRepository
from payments.application.ports.clock import Clock
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.operation_locks import OperationLockRepository
from payments.application.ports.payment_customers import PaymentCustomerRepository
from payments.application.ports.provider import PaymentProvider
from payments.application.ports.subscriptions import SubscriptionCheckoutRepository
from payments.application.ports.unit_of_work import (
    SubscriptionConfirmUnitOfWorkFactory,
)
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.ids import generate_uuid_id
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.subscription import Subscription

SUBSCRIPTION_CHECKOUT_IDEMPOTENCY_SCOPE = "subscriptions-checkout"
SUBSCRIPTION_CONFIRM_IDEMPOTENCY_SCOPE = "subscriptions-confirm"


@dataclass(frozen=True, slots=True)
class SubscriptionCheckoutCommand:
    plan_id: str
    success_url: str
    fail_url: str


@dataclass(frozen=True, slots=True)
class SubscriptionCheckoutResult:
    subscription_id: str
    customer_key: str
    product_code: str
    amount: int
    currency: str
    order_name: str
    client_key: str
    success_url: str
    fail_url: str


@dataclass(frozen=True, slots=True)
class SubscriptionConfirmCommand:
    subscription_id: str
    customer_key: str
    auth_key: str


@dataclass(frozen=True, slots=True)
class SubscriptionConfirmResult:
    subscription_id: str
    status: str
    payment_status: str
    payment_id: str
    invoice_id: str
    next_billing_date: datetime | None


async def create_subscription_checkout(
    requester: RequestContext,
    command: SubscriptionCheckoutCommand,
    catalog: CatalogRepository,
    subscriptions: SubscriptionCheckoutRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    clock: Clock,
    client_key: str,
    idempotency_key: str | None = None,
) -> SubscriptionCheckoutResult:
    """구독 체크아웃 초안과 Toss SDK 입력값을 생성합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        command: 구독할 플랜과 성공/실패 URL입니다.
        catalog: 활성 구독 플랜 조회 저장소입니다.
        subscriptions: 구독 초안 저장소입니다.
        payment_customers: Toss customerKey 조회/생성 저장소입니다.
        idempotency_keys: 중복 checkout 생성을 막는 멱등키 저장소입니다.
        clock: 생성 시각을 제공하는 포트입니다.
        client_key: 프론트가 Toss SDK에 전달할 클라이언트 키입니다.
        idempotency_key: 같은 checkout 생성 요청을 묶는 선택 멱등키입니다.

    Returns:
        구독 체크아웃과 빌링 인증 시작에 필요한 값입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
        InvalidStateTransitionError: 플랜이 판매 가능하지 않거나 같은 상품의
            활성 구독이 이미 있을 때 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    payload = {
        "planId": command.plan_id,
        "successUrl": command.success_url,
        "failUrl": command.fail_url,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    now = clock.utc_now()
    if key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            SUBSCRIPTION_CHECKOUT_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _subscription_checkout_result_from_response_body(
                existing_key.response_body
            )
    catalog_row = await catalog.get_active_subscription_plan(command.plan_id)
    if catalog_row is None:
        raise InvalidStateTransitionError("subscription plan is not available")
    product, plan = catalog_row
    active_count = await subscriptions.count_active_subscriptions_for_user_product(
        requester.user_id,
        product.product_code,
    )
    if active_count > 0:
        raise InvalidStateTransitionError("active subscription already exists")

    payment_customer = await payment_customers.get_active_payment_customer_for_user(
        requester.user_id
    )
    if payment_customer is None:
        payment_customer = PaymentCustomer(
            id=PaymentCustomer.generate_id(),
            user_id=requester.user_id,
            provider="tosspayments",
            customer_key=PaymentCustomer.generate_pcus_key(),
            status="active",
        )
        await payment_customers.save_payment_customer(payment_customer)

    subscription = Subscription(
        id=Subscription.generate_id(),
        user_id=requester.user_id,
        payment_customer_id=payment_customer.id,
        plan_id=plan.id,
        product_code=product.product_code,
        status="pending",
        cancel_at_period_end=False,
    )
    await subscriptions.save_subscription(subscription)

    result = SubscriptionCheckoutResult(
        subscription_id=subscription.id,
        customer_key=payment_customer.customer_key,
        product_code=product.product_code,
        amount=plan.amount,
        currency=plan.currency,
        order_name=f"{product.name} {plan.billing_period} subscription",
        client_key=client_key,
        success_url=_append_subscription_id(command.success_url, subscription.id),
        fail_url=_append_subscription_id(command.fail_url, subscription.id),
    )
    if key_hash is not None:
        await idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=IdempotencyKey.generate_id(),
                scope=SUBSCRIPTION_CHECKOUT_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="succeeded",
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=24),
                resource_type="subscription",
                resource_id=subscription.id,
                response_status=201,
                response_body=_subscription_checkout_result_to_response_body(result),
            )
        )
    return result


async def confirm_subscription_checkout(
    requester: RequestContext,
    command: SubscriptionConfirmCommand,
    catalog: CatalogRepository,
    subscriptions: SubscriptionCheckoutRepository,
    billing_auths: BillingAuthRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    provider: PaymentProvider,
    clock: Clock,
    billing_key_cipher: BillingKeyCipher,
    subscription_confirm_uow_factory: SubscriptionConfirmUnitOfWorkFactory,
    idempotency_key: str,
    operation_locks: OperationLockRepository | None = None,
) -> SubscriptionConfirmResult:
    """빌링 인증 성공 후 첫 결제를 실행하고 구독을 활성화합니다."""
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    user_id = requester.user_id
    payload = {
        "subscriptionId": command.subscription_id,
        "customerKey": command.customer_key,
        "authKey": command.auth_key,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key)
    now = clock.utc_now()
    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"subscriptions-confirm:{command.subscription_id}",
        fencing_counter_key="subscriptions-confirm",
        now=now,
        metadata={
            "subscriptionId": command.subscription_id,
            "requestId": requester.request_id,
            "userId": user_id,
            "idempotencyKeyHash": key_hash,
        },
    )
    try:
        return await _confirm_subscription_checkout_locked(
            requester=requester,
            command=command,
            catalog=catalog,
            subscriptions=subscriptions,
            billing_auths=billing_auths,
            payment_customers=payment_customers,
            idempotency_keys=idempotency_keys,
            provider=provider,
            clock=clock,
            billing_key_cipher=billing_key_cipher,
            subscription_confirm_uow_factory=subscription_confirm_uow_factory,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            key_hash=key_hash,
            now=now,
            user_id=user_id,
        )
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def _confirm_subscription_checkout_locked(
    *,
    requester: RequestContext,
    command: SubscriptionConfirmCommand,
    catalog: CatalogRepository,
    subscriptions: SubscriptionCheckoutRepository,
    billing_auths: BillingAuthRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    provider: PaymentProvider,
    clock: Clock,
    billing_key_cipher: BillingKeyCipher,
    subscription_confirm_uow_factory: SubscriptionConfirmUnitOfWorkFactory,
    idempotency_key: str,
    request_hash: str,
    key_hash: str,
    now: datetime,
    user_id: str,
) -> SubscriptionConfirmResult:
    subscription = await subscriptions.get_subscription(command.subscription_id)
    if subscription is None:
        raise ResourceNotFoundError("subscription was not found")
    if subscription.user_id != user_id:
        raise ForbiddenError("subscription belongs to another user")
    existing_key = await idempotency_keys.find_idempotency_key(
        SUBSCRIPTION_CONFIRM_IDEMPOTENCY_SCOPE,
        key_hash,
    )
    if existing_key is not None and existing_key.request_hash != request_hash:
        raise IdempotencyConflictError("idempotency key was used with another payload")
    if existing_key is not None and existing_key.response_body is not None:
        if existing_key.response_status == 402:
            raise PaymentRequiredResponseError(
                "subscription confirmation failed",
                existing_key.response_body,
            )
        return _subscription_confirm_result_from_response_body(
            existing_key.response_body
        )
    if existing_key is not None and existing_key.status == "processing":
        raise InvalidStateTransitionError("subscription confirmation is processing")
    existing_subscription_success = (
        await idempotency_keys.find_succeeded_idempotency_key_by_resource(
            SUBSCRIPTION_CONFIRM_IDEMPOTENCY_SCOPE,
            "subscription",
            subscription.id,
        )
    )
    if existing_subscription_success is not None:
        if existing_subscription_success.request_hash != request_hash:
            raise IdempotencyConflictError(
                "subscription was confirmed with another payload"
            )
        if existing_subscription_success.response_body is not None:
            return _subscription_confirm_result_from_response_body(
                existing_subscription_success.response_body
            )
    if subscription.status != "pending":
        raise InvalidStateTransitionError("subscription cannot be confirmed")
    payment_customer = await payment_customers.get_active_payment_customer_for_user(
        user_id
    )
    if (
        payment_customer is None
        or payment_customer.id != subscription.payment_customer_id
        or command.customer_key != payment_customer.customer_key
    ):
        raise InvalidStateTransitionError("customerKey does not match subscription")
    catalog_row = await catalog.get_active_subscription_plan(subscription.plan_id)
    if catalog_row is None:
        raise ResourceNotFoundError("subscription plan not found")
    product, plan = catalog_row

    processing_key = IdempotencyKey(
        id=(
            existing_key.id
            if existing_key is not None
            else IdempotencyKey.generate_id()
        ),
        scope=SUBSCRIPTION_CONFIRM_IDEMPOTENCY_SCOPE,
        key_hash=key_hash,
        request_hash=request_hash,
        status="processing",
        created_at=existing_key.created_at if existing_key is not None else now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
        resource_type="subscription",
        resource_id=subscription.id,
        locked_until_at=now + timedelta(minutes=5),
    )
    await idempotency_keys.save_idempotency_key(processing_key)

    try:
        issued = await provider.issue_billing_key(
            auth_key=command.auth_key,
            customer_key=command.customer_key,
        )
    except ProviderError as exc:
        response_body = _billing_key_issue_failure_body(subscription, exc)
        await _save_failed_subscription_confirm_idempotency_key(
            idempotency_keys=idempotency_keys,
            processing_key=processing_key,
            request_hash=request_hash,
            now=clock.utc_now(),
            response_body=response_body,
        )
        raise PaymentRequiredResponseError(
            "billing key issue failed",
            response_body,
        ) from exc
    instrument = PaymentInstrument(
        id=PaymentInstrument.generate_id(),
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=billing_key_cipher.encrypt(issued.billing_key),
        billing_key_hash=sha256(issued.billing_key.encode()).hexdigest(),
        status="active",
        provider_raw=issued.response_summary,
    )
    is_default = await billing_auths.count_active_billing_methods_for_user(user_id) == 0
    billing_method = BillingMethod(
        id=BillingMethod.generate_id(),
        user_id=user_id,
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name=f"{issued.card_company} {issued.masked_card_number}",
        provider="tosspayments",
        is_default=is_default,
        status="active",
        method=issued.method,
        card_company=issued.card_company,
        billing_key_status="active",
        created_at=now,
        masked_number=issued.masked_card_number,
    )

    billing_cycle_key = Payment.generate_billing_cycle_key(subscription.id, now)
    payment = Payment(
        id=Payment.generate_id(),
        order_id=generate_uuid_id("ord"),
        amount=plan.amount,
        status="ready",
        created_at=now,
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
        payment_customer_id=payment_customer.id,
        billing_method_id=billing_method.id,
    )
    try:
        charged = await provider.charge_billing_key(
            billing_key=issued.billing_key,
            customer_key=command.customer_key,
            order_id=payment.order_id,
            amount=plan.amount,
            order_name=f"{product.name} {plan.billing_period} subscription",
            idempotency_key=idempotency_key,
        )
    except ProviderError as exc:
        response_body = await _record_first_payment_failure(
            requester_user_id=user_id,
            subscription=subscription,
            subscriptions=subscriptions,
            payment=payment,
            instrument=instrument,
            billing_method=billing_method,
            is_default=is_default,
            subscription_confirm_uow_factory=subscription_confirm_uow_factory,
            processing_key=processing_key,
            request_hash=request_hash,
            now=now,
            updated_at=clock.utc_now(),
            error=exc,
        )
        raise PaymentRequiredResponseError(
            "first subscription payment failed",
            response_body,
        ) from exc
    if charged.order_id != payment.order_id or charged.amount != payment.amount:
        response_body = await _record_first_payment_failure(
            requester_user_id=user_id,
            subscription=subscription,
            subscriptions=subscriptions,
            payment=payment,
            instrument=instrument,
            billing_method=billing_method,
            is_default=is_default,
            subscription_confirm_uow_factory=subscription_confirm_uow_factory,
            processing_key=processing_key,
            request_hash=request_hash,
            now=now,
            updated_at=clock.utc_now(),
            error=ProviderError(
                "provider billing charge response does not match request",
                provider_code="PROVIDER_BILLING_CHARGE_MISMATCH",
            ),
        )
        raise PaymentRequiredResponseError(
            "first subscription payment failed",
            response_body,
        )
    payment.status = "paid"
    payment.payment_key = charged.payment_key
    payment.approved_at = charged.approved_at
    payment.receipt_url = charged.receipt_url
    payment.method = charged.method
    payment.method_detail = charged.method_detail
    payment.provider_response_summary = charged.response_summary
    payment.cancelable_amount = charged.amount

    invoice = await _first_payment_invoice(
        subscriptions=subscriptions,
        subscription=subscription,
        payment=payment,
        status="paid",
        issued_at=now,
        receipt_url=charged.receipt_url,
    )
    subscription.status = "active"
    subscription.billing_anchor_day = now.day
    subscription.current_period_start_at = now
    subscription.current_period_end_at = next_billing_at(
        now,
        plan.billing_period,
        subscription.billing_anchor_day,
    )
    subscription.next_billing_at = subscription.current_period_end_at

    result = SubscriptionConfirmResult(
        subscription_id=subscription.id,
        status=subscription.status,
        payment_status=payment.status,
        payment_id=payment.id,
        invoice_id=invoice.id,
        next_billing_date=subscription.next_billing_at,
    )
    async with subscription_confirm_uow_factory() as uow:
        if is_default:
            await uow.billing_auths.clear_default_billing_methods_for_user(
                user_id
            )
        await uow.billing_auths.save_payment_instrument(instrument)
        await uow.billing_auths.save_billing_method(billing_method)
        await uow.subscriptions.save_payment(payment)
        await uow.subscriptions.save_invoice(invoice)
        await uow.subscriptions.save_subscription(subscription)
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=processing_key.id,
                scope=SUBSCRIPTION_CONFIRM_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="succeeded",
                created_at=processing_key.created_at,
                updated_at=clock.utc_now(),
                expires_at=now + timedelta(hours=24),
                resource_type="subscription",
                resource_id=subscription.id,
                response_status=200,
                response_body=_subscription_confirm_result_to_response_body(result),
            )
        )
    return result

async def _save_failed_subscription_confirm_idempotency_key(
    *,
    idempotency_keys: IdempotencyKeyRepository,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    response_body: dict[str, object],
) -> None:
    await idempotency_keys.save_idempotency_key(
        _failed_subscription_confirm_idempotency_key(
            processing_key=processing_key,
            request_hash=request_hash,
            now=now,
            response_body=response_body,
        )
    )


def _failed_subscription_confirm_idempotency_key(
    *,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    response_body: dict[str, object],
) -> IdempotencyKey:
    return IdempotencyKey(
        id=processing_key.id,
        scope=SUBSCRIPTION_CONFIRM_IDEMPOTENCY_SCOPE,
        key_hash=processing_key.key_hash,
        request_hash=request_hash,
        status="failed",
        created_at=processing_key.created_at,
        updated_at=now,
        expires_at=processing_key.expires_at,
        resource_type=processing_key.resource_type,
        resource_id=processing_key.resource_id,
        response_status=402,
        response_body=response_body,
    )


def _billing_key_issue_failure_body(
    subscription: Subscription,
    error: ProviderError,
) -> dict[str, object]:
    return {
        "subscriptionId": subscription.id,
        "status": subscription.status,
        "failure": {
            "code": "BILLING_KEY_ISSUE_FAILED",
            "providerCode": (
                error.provider_code or "PROVIDER_BILLING_KEY_ISSUE_FAILED"
            ),
            "message": str(error),
            "retryable": error.retryable,
        },
    }


def _first_payment_failure_snapshot(error: ProviderError) -> dict[str, object]:
    return {
        "phase": "confirm",
        "reason": "provider_rejected" if error.provider_code else "provider_error",
        "providerCode": error.provider_code or "PROVIDER_BILLING_CHARGE_FAILED",
        "message": str(error),
        "retryable": error.retryable,
    }


async def _record_first_payment_failure(
    *,
    requester_user_id: str,
    subscription: Subscription,
    subscriptions: SubscriptionCheckoutRepository,
    payment: Payment,
    instrument: PaymentInstrument,
    billing_method: BillingMethod,
    is_default: bool,
    subscription_confirm_uow_factory: SubscriptionConfirmUnitOfWorkFactory,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    updated_at: datetime,
    error: ProviderError,
) -> dict[str, object]:
    invoice = await _first_payment_invoice(
        subscriptions=subscriptions,
        subscription=subscription,
        payment=payment,
        status="issued",
        issued_at=now,
    )
    payment.status = "failed"
    payment.failure = _first_payment_failure_snapshot(error)
    response_body = _first_payment_failure_body(
        subscription=subscription,
        payment=payment,
        invoice=invoice,
        error=error,
    )
    async with subscription_confirm_uow_factory() as uow:
        if is_default:
            await uow.billing_auths.clear_default_billing_methods_for_user(
                requester_user_id
            )
        await uow.billing_auths.save_payment_instrument(instrument)
        await uow.billing_auths.save_billing_method(billing_method)
        await uow.subscriptions.save_payment(payment)
        await uow.subscriptions.save_invoice(invoice)
        await uow.subscriptions.save_subscription(subscription)
        await uow.idempotency_keys.save_idempotency_key(
            _failed_subscription_confirm_idempotency_key(
                processing_key=processing_key,
                request_hash=request_hash,
                now=updated_at,
                response_body=response_body,
            )
        )
    return response_body


async def _first_payment_invoice(
    *,
    subscriptions: SubscriptionCheckoutRepository,
    subscription: Subscription,
    payment: Payment,
    status: Literal["issued", "paid"],
    issued_at: datetime,
    receipt_url: str | None = None,
) -> Invoice:
    invoice = None
    if payment.billing_cycle_key is not None:
        invoice = await subscriptions.get_open_invoice_for_subscription_cycle(
            subscription.id,
            payment.billing_cycle_key,
        )
    if invoice is None:
        invoice = Invoice(
            id=Invoice.generate_id(),
            user_id=subscription.user_id,
            payment_id=payment.id,
            status=status,
            issued_at=issued_at,
            subscription_id=subscription.id,
            billing_cycle_key=payment.billing_cycle_key,
            receipt_url=receipt_url,
        )
    else:
        invoice.payment_id = payment.id
        invoice.status = status
        invoice.receipt_url = receipt_url
    return invoice


def _first_payment_failure_body(
    *,
    subscription: Subscription,
    payment: Payment,
    invoice: Invoice,
    error: ProviderError,
) -> dict[str, object]:
    return {
        "subscriptionId": subscription.id,
        "status": subscription.status,
        "paymentStatus": payment.status,
        "paymentId": payment.id,
        "invoiceId": invoice.id,
        "failure": {
            "code": "FIRST_PAYMENT_FAILED",
            "providerCode": error.provider_code or "PROVIDER_BILLING_CHARGE_FAILED",
            "message": str(error),
            "retryable": error.retryable,
        },
    }


def _append_subscription_id(url: str, subscription_id: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["subscriptionId"] = subscription_id
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _subscription_checkout_result_to_response_body(
    result: SubscriptionCheckoutResult,
) -> dict[str, object]:
    return {
        "subscriptionId": result.subscription_id,
        "customerKey": result.customer_key,
        "productCode": result.product_code,
        "amount": result.amount,
        "currency": result.currency,
        "orderName": result.order_name,
        "clientKey": result.client_key,
        "successUrl": result.success_url,
        "failUrl": result.fail_url,
    }


def _subscription_checkout_result_from_response_body(
    body: dict[str, object],
) -> SubscriptionCheckoutResult:
    amount = body["amount"]
    if not isinstance(amount, int):
        raise InvalidStateTransitionError("idempotency response amount is invalid")
    return SubscriptionCheckoutResult(
        subscription_id=str(body["subscriptionId"]),
        customer_key=str(body["customerKey"]),
        product_code=str(body["productCode"]),
        amount=amount,
        currency=str(body["currency"]),
        order_name=str(body["orderName"]),
        client_key=str(body["clientKey"]),
        success_url=str(body["successUrl"]),
        fail_url=str(body["failUrl"]),
    )


def _subscription_confirm_result_to_response_body(
    result: SubscriptionConfirmResult,
) -> dict[str, object]:
    return {
        "subscriptionId": result.subscription_id,
        "status": result.status,
        "paymentStatus": result.payment_status,
        "paymentId": result.payment_id,
        "invoiceId": result.invoice_id,
        "nextBillingDate": result.next_billing_date,
    }


def _subscription_confirm_result_from_response_body(
    body: dict[str, object],
) -> SubscriptionConfirmResult:
    next_billing_date = body["nextBillingDate"]
    if next_billing_date is not None and not isinstance(next_billing_date, datetime):
        raise InvalidStateTransitionError(
            "idempotency response next billing date is invalid"
        )
    return SubscriptionConfirmResult(
        subscription_id=str(body["subscriptionId"]),
        status=str(body["status"]),
        payment_status=str(body["paymentStatus"]),
        payment_id=str(body["paymentId"]),
        invoice_id=str(body["invoiceId"]),
        next_billing_date=next_billing_date,
    )


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
