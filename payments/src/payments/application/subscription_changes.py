from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, NoReturn

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    BadRequestError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentRequiredResponseError,
    ProviderError,
    ResourceNotFoundError,
)
from payments.application.notifications import (
    NotificationEnqueueDependencies,
    enqueue_user_notification_if_available,
)
from payments.application.operation_locks import (
    acquire_required_operation_lock,
    release_operation_lock,
)
from payments.application.ports.billing_keys import BillingKeyCipher
from payments.application.ports.billing_retry import BillingRetryRepository
from payments.application.ports.catalog import CatalogRepository
from payments.application.ports.clock import Clock
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.operation_locks import OperationLockRepository
from payments.application.ports.operator_audits import OperatorAuditRepository
from payments.application.ports.payment_customers import PaymentCustomerRepository
from payments.application.ports.provider import PaymentProvider
from payments.application.ports.subscription_changes import (
    SubscriptionChangeTokenCodec,
)
from payments.application.ports.subscriptions import SubscriptionAccountRepository
from payments.application.ports.unit_of_work import SubscriptionChangeUnitOfWorkFactory
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.ids import generate_uuid_id
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.notification import TemplateArgs
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_change_preview import (
    SubscriptionChangePreview,
)
from payments.domain.entities.subscription_plan import SubscriptionPlan

SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE = "subscriptions-change"
SUBSCRIPTION_CHANGE_PREVIEW_IDEMPOTENCY_SCOPE = "subscriptions-change-preview"
SUBSCRIPTION_CHANGE_CONFIRMATION_RESOURCE = "subscription_change_confirmation"


@dataclass(frozen=True, slots=True)
class SubscriptionChangePreviewCommand:
    target_plan_id: str


@dataclass(frozen=True, slots=True)
class SubscriptionChangePreviewResult:
    subscription_id: str
    product_code: str
    current_plan_id: str
    target_plan_id: str
    server_decision: Literal["upgrade", "downgrade"]
    will_apply: Literal["immediate", "next_billing_date"]
    confirmation_token: str
    confirmation_expires_at: datetime
    immediate_payment: dict[str, object] | None
    effective_at: datetime | None
    next_billing_date: datetime | None
    notice: str


@dataclass(frozen=True, slots=True)
class SubscriptionChangeCommand:
    confirmation_token: str
    confirmed: bool


@dataclass(frozen=True, slots=True)
class SubscriptionChangeResult:
    subscription_id: str
    product_code: str
    status: str
    server_decision: Literal["upgrade", "downgrade"]
    plan_id: str
    previous_plan_id: str
    applied_at: datetime | None
    next_billing_date: datetime | None
    payment: dict[str, object] | None
    notification: dict[str, object] | None
    pending_plan: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class PlanUpgradeChargeResult:
    payment: Payment
    invoice: Invoice
    payment_result: dict[str, object]
    notification: dict[str, object]


class _PlanUpgradePaymentRequired(PaymentRequiredResponseError):
    def __init__(
        self,
        message: str,
        result: SubscriptionChangeResult,
        payment: Payment,
        invoice: Invoice,
    ) -> None:
        super().__init__(message, _change_result_to_response_body(result))
        self.result = result
        self.payment = payment
        self.invoice = invoice


async def create_subscription_change_preview(
    requester: RequestContext,
    subscription_id: str,
    command: SubscriptionChangePreviewCommand,
    subscriptions: SubscriptionAccountRepository,
    catalog: CatalogRepository,
    token_codec: SubscriptionChangeTokenCodec,
    clock: Clock,
    idempotency_keys: IdempotencyKeyRepository | None = None,
    idempotency_key: str | None = None,
) -> SubscriptionChangePreviewResult:
    """구독 플랜 변경 전 사용자 확인용 미리보기를 생성합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        subscription_id: 변경할 구독 ID입니다.
        command: 목표 플랜 ID입니다.
        subscriptions: 회원 구독 조회 저장소입니다.
        catalog: 활성 플랜 조회 저장소입니다.
        token_codec: 확인 토큰 생성/검증 포트입니다.
        clock: 토큰 생성/만료 시각을 제공하는 포트입니다.
        idempotency_keys: 선택 멱등키 저장소입니다.
        idempotency_key: 같은 미리보기 요청을 재사용하기 위한 선택 키입니다.

    Returns:
        서버 판정, 적용 시점, 결제 안내와 확인 토큰입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
        ResourceNotFoundError: 구독 또는 플랜을 찾을 수 없는 경우 발생합니다.
        ForbiddenError: 구독이 현재 회원 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 변경 불가능한 구독 상태 또는 다른 상품
            플랜으로 변경하려는 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    now = clock.utc_now()
    payload = {
        "userId": requester.user_id,
        "subscriptionId": subscription_id,
        "targetPlanId": command.target_plan_id,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    if idempotency_keys is not None and key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            SUBSCRIPTION_CHANGE_PREVIEW_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _preview_result_from_response_body(existing_key.response_body)

    subscription = await _get_owned_subscription(
        subscriptions,
        subscription_id,
        requester.user_id,
    )
    if subscription.status != "active":
        raise InvalidStateTransitionError("subscription plan cannot be changed")

    current_row = await catalog.get_active_subscription_plan(subscription.plan_id)
    target_row = await catalog.get_active_subscription_plan(command.target_plan_id)
    if current_row is None or target_row is None:
        raise ResourceNotFoundError("subscription plan was not found")
    current_product, current_plan = current_row
    target_product, target_plan = target_row
    if target_product.product_code != subscription.product_code:
        raise InvalidStateTransitionError("target plan must belong to same product")
    if current_product.product_code != subscription.product_code:
        raise InvalidStateTransitionError("current plan does not match subscription")

    decision: Literal["upgrade", "downgrade"] = (
        "upgrade" if target_plan.amount > current_plan.amount else "downgrade"
    )
    amount = _upgrade_proration_amount(
        target_plan.amount - current_plan.amount,
        subscription=subscription,
        calculated_at=now,
    )
    expires_at = now + timedelta(minutes=10)
    next_billing_date = (
        subscription.next_billing_at or subscription.current_period_end_at
    )
    preview = SubscriptionChangePreview(
        confirmation_token="",
        subscription_id=subscription.id,
        user_id=requester.user_id,
        product_code=subscription.product_code,
        current_plan_id=current_plan.id,
        target_plan_id=target_plan.id,
        server_decision=decision,
        will_apply="immediate" if decision == "upgrade" else "next_billing_date",
        amount=amount,
        currency=target_plan.currency,
        next_billing_date=next_billing_date,
        expires_at=expires_at,
        created_at=now,
    )
    preview.confirmation_token = token_codec.encode_plan_change_preview(preview)
    result = _preview_result(preview)
    if idempotency_keys is not None and key_hash is not None:
        await idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=IdempotencyKey.generate_id(),
                scope=SUBSCRIPTION_CHANGE_PREVIEW_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="succeeded",
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
                resource_type="subscription",
                resource_id=subscription.id,
                response_status=200,
                response_body=_preview_result_to_response_body(result),
            )
        )
    return result


async def execute_subscription_change(
    requester: RequestContext,
    subscription_id: str,
    command: SubscriptionChangeCommand,
    subscriptions: SubscriptionAccountRepository,
    catalog: CatalogRepository,
    token_codec: SubscriptionChangeTokenCodec,
    billing_repository: BillingRetryRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    provider: PaymentProvider,
    clock: Clock,
    billing_key_cipher: BillingKeyCipher,
    idempotency_key: str,
    operation_locks: OperationLockRepository | None = None,
    operator_audits: OperatorAuditRepository | None = None,
    subscription_change_uow_factory: SubscriptionChangeUnitOfWorkFactory | None = None,
    notification_dependencies: NotificationEnqueueDependencies | None = None,
) -> SubscriptionChangeResult:
    """미리보기 확인 토큰으로 구독 플랜 변경을 최종 실행합니다."""
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    payload = {
        "subscriptionId": subscription_id,
        "confirmationToken": command.confirmation_token,
        "confirmed": command.confirmed,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key)
    now = clock.utc_now()
    existing_key = await idempotency_keys.find_idempotency_key(
        SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE,
        key_hash,
    )
    if existing_key is not None and existing_key.request_hash != request_hash:
        raise IdempotencyConflictError("idempotency key was used with another payload")
    if existing_key is not None and existing_key.response_body is not None:
        if existing_key.response_status == 402:
            raise PaymentRequiredResponseError(
                "subscription plan change payment failed",
                existing_key.response_body,
            )
        return _change_result_from_response_body(existing_key.response_body)
    if existing_key is not None and existing_key.status == "processing":
        raise InvalidStateTransitionError("subscription change is processing")
    if not command.confirmed:
        raise BadRequestError("confirmed must be true")
    subscription = await _get_owned_subscription(
        subscriptions,
        subscription_id,
        requester.user_id,
    )
    if subscription.status != "active":
        raise InvalidStateTransitionError("subscription plan cannot be changed")
    preview = token_codec.decode_plan_change_preview(command.confirmation_token)
    if preview is None or preview.expires_at <= clock.utc_now():
        raise InvalidStateTransitionError("confirmation token expired")
    if (
        preview.subscription_id != subscription.id
        or preview.user_id != requester.user_id
    ):
        raise InvalidStateTransitionError("confirmation token does not match")
    if preview.product_code != subscription.product_code:
        raise InvalidStateTransitionError("confirmation token does not match")
    if preview.current_plan_id != subscription.plan_id:
        raise InvalidStateTransitionError("subscription changed after preview")

    existing_confirmation_key = await idempotency_keys.find_idempotency_key_by_resource(
        SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE,
        SUBSCRIPTION_CHANGE_CONFIRMATION_RESOURCE,
        preview.confirmation_token,
    )
    if existing_confirmation_key is not None:
        return _change_result_from_existing_idempotency_key(existing_confirmation_key)

    target_row = await catalog.get_active_subscription_plan(preview.target_plan_id)
    if target_row is None:
        raise ResourceNotFoundError("subscription plan was not found")
    target_product, target_plan = target_row
    current_row = await catalog.get_active_subscription_plan(subscription.plan_id)
    if current_row is None:
        raise InvalidStateTransitionError("subscription changed after preview")
    current_product, current_plan = current_row
    if current_product.product_code != subscription.product_code:
        raise InvalidStateTransitionError("subscription changed after preview")
    if target_product.product_code != subscription.product_code:
        raise InvalidStateTransitionError("target plan must belong to same product")
    _validate_change_preview_still_current(
        preview=preview,
        subscription=subscription,
        current_plan=current_plan,
        target_plan=target_plan,
    )

    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"subscription:{subscription_id}",
        fencing_counter_key="subscription",
        now=now,
        metadata={
            "api": SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE,
            "request_id": requester.request_id,
            "subscription_id": subscription_id,
        },
    )
    try:
        locked_subscription = await _get_owned_subscription(
            subscriptions,
            subscription_id,
            requester.user_id,
        )
        if locked_subscription.status != "active":
            raise InvalidStateTransitionError("subscription plan cannot be changed")
        if preview.current_plan_id != locked_subscription.plan_id:
            raise InvalidStateTransitionError("subscription changed after preview")
        existing_confirmation_key = (
            await idempotency_keys.find_idempotency_key_by_resource(
                SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE,
                SUBSCRIPTION_CHANGE_CONFIRMATION_RESOURCE,
                preview.confirmation_token,
            )
        )
        if existing_confirmation_key is not None:
            return _change_result_from_existing_idempotency_key(
                existing_confirmation_key
            )
        subscription = locked_subscription
        _validate_change_preview_still_current(
            preview=preview,
            subscription=subscription,
            current_plan=current_plan,
            target_plan=target_plan,
        )
        processing_key = IdempotencyKey(
            id=(
                existing_key.id
                if existing_key is not None
                else IdempotencyKey.generate_id()
            ),
            scope=SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            status="processing",
            created_at=existing_key.created_at if existing_key is not None else now,
            updated_at=now,
            expires_at=now + timedelta(hours=24),
            resource_type=SUBSCRIPTION_CHANGE_CONFIRMATION_RESOURCE,
            resource_id=preview.confirmation_token,
            locked_until_at=now + timedelta(minutes=5),
        )
        await idempotency_keys.save_idempotency_key(processing_key)

        previous_plan_id = subscription.plan_id
        previous_state = _subscription_change_previous_state(subscription)
        if target_plan.id == subscription.plan_id:
            result = SubscriptionChangeResult(
                subscription_id=subscription.id,
                product_code=subscription.product_code,
                status=subscription.status,
                server_decision=preview.server_decision,
                plan_id=subscription.plan_id,
                previous_plan_id=previous_plan_id,
                applied_at=None,
                next_billing_date=subscription.next_billing_at,
                payment=None,
                notification=None,
                pending_plan=None,
            )
            await _save_subscription_change_success(
                subscriptions=subscriptions,
                billing_repository=billing_repository,
                idempotency_keys=idempotency_keys,
                operator_audits=operator_audits,
                subscription_change_uow_factory=subscription_change_uow_factory,
                subscription=subscription,
                result=result,
                processing_key=processing_key,
                request_hash=request_hash,
                now=clock.utc_now(),
                user_id=requester.user_id,
                previous_state=previous_state,
                target_plan_id=target_plan.id,
                payment_result=None,
                payment=None,
                invoice=None,
            )
            return result
        if preview.server_decision == "downgrade":
            subscription.pending_plan_id = target_plan.id
            subscription.pending_plan_effective_at = preview.next_billing_date
            result = SubscriptionChangeResult(
                subscription_id=subscription.id,
                product_code=subscription.product_code,
                status=subscription.status,
                server_decision=preview.server_decision,
                plan_id=subscription.plan_id,
                previous_plan_id=previous_plan_id,
                applied_at=None,
                next_billing_date=subscription.next_billing_at,
                payment=None,
                notification=None,
                pending_plan={
                    "planId": target_plan.id,
                    "planName": _plan_name(
                        target_product.name,
                        target_plan.billing_period,
                    ),
                    "effectiveAt": preview.next_billing_date,
                },
            )
            await _save_subscription_change_success(
                subscriptions=subscriptions,
                billing_repository=billing_repository,
                idempotency_keys=idempotency_keys,
                operator_audits=operator_audits,
                subscription_change_uow_factory=subscription_change_uow_factory,
                subscription=subscription,
                result=result,
                processing_key=processing_key,
                request_hash=request_hash,
                now=clock.utc_now(),
                user_id=requester.user_id,
                previous_state=previous_state,
                target_plan_id=target_plan.id,
                payment_result=None,
                payment=None,
                invoice=None,
            )
            return result

        charge_result: PlanUpgradeChargeResult | None = None
        notification: dict[str, object] | None = None
        if preview.amount > 0:
            try:
                charge_result = await _charge_plan_upgrade(
                    requester.user_id,
                    preview,
                    subscription.payment_customer_id,
                    target_product.name,
                    billing_repository,
                    payment_customers,
                    provider,
                    billing_key_cipher,
                    now,
                )
                notification = charge_result.notification
            except _PlanUpgradePaymentRequired as exc:
                failed_result = exc.result
                await _save_subscription_change_failure(
                    billing_repository=billing_repository,
                    idempotency_keys=idempotency_keys,
                    operator_audits=operator_audits,
                    subscription_change_uow_factory=(
                        subscription_change_uow_factory
                    ),
                    result=failed_result,
                    payment=exc.payment,
                    invoice=exc.invoice,
                    processing_key=processing_key,
                    request_hash=request_hash,
                    now=clock.utc_now(),
                    user_id=requester.user_id,
                    previous_state=previous_state,
                    target_plan_id=target_plan.id,
                    payment_result=failed_result.payment,
                )
                raise

        subscription.plan_id = target_plan.id
        subscription.pending_plan_id = None
        subscription.pending_plan_effective_at = None
        result = SubscriptionChangeResult(
            subscription_id=subscription.id,
            product_code=subscription.product_code,
            status=subscription.status,
            server_decision=preview.server_decision,
            plan_id=subscription.plan_id,
            previous_plan_id=previous_plan_id,
            applied_at=clock.utc_now(),
            next_billing_date=subscription.next_billing_at,
            payment=(
                {
                    "invoiceId": None,
                    "paymentId": None,
                    "status": "paid",
                    "amount": preview.amount,
                    "currency": preview.currency,
                    "receiptUrl": None,
                }
                if preview.amount == 0
                else (
                    charge_result.payment_result
                    if charge_result is not None
                    else None
                )
            ),
            notification=notification,
            pending_plan=None,
        )
        await _save_subscription_change_success(
            subscriptions=subscriptions,
            billing_repository=billing_repository,
            idempotency_keys=idempotency_keys,
            operator_audits=operator_audits,
            subscription_change_uow_factory=subscription_change_uow_factory,
            subscription=subscription,
            result=result,
            processing_key=processing_key,
            request_hash=request_hash,
            now=clock.utc_now(),
            user_id=requester.user_id,
            previous_state=previous_state,
            target_plan_id=target_plan.id,
            payment_result=result.payment,
            payment=charge_result.payment if charge_result is not None else None,
            invoice=charge_result.invoice if charge_result is not None else None,
        )
        if charge_result is not None:
            await _enqueue_subscription_plan_upgrade_receipt(
                user_id=requester.user_id,
                subscription=subscription,
                from_plan_name=_plan_name(
                    current_product.name,
                    current_plan.billing_period,
                ),
                to_plan_name=_plan_name(
                    target_product.name,
                    target_plan.billing_period,
                ),
                changed_at=result.applied_at or clock.utc_now(),
                currency=preview.currency,
                charge_result=charge_result,
                notification_dependencies=notification_dependencies,
            )
        return result
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def _charge_plan_upgrade(
    user_id: str,
    preview: SubscriptionChangePreview,
    payment_customer_id: str,
    product_name: str,
    billing_repository: BillingRetryRepository,
    payment_customers: PaymentCustomerRepository,
    provider: PaymentProvider,
    billing_key_cipher: BillingKeyCipher,
    now: datetime,
) -> PlanUpgradeChargeResult:
    payment_customer = await payment_customers.get_active_payment_customer_for_user(
        user_id
    )
    if payment_customer is None or payment_customer.id != payment_customer_id:
        raise InvalidStateTransitionError("payment customer does not match")
    billing_method = await billing_repository.get_default_billing_method(user_id)
    if billing_method is None:
        raise InvalidStateTransitionError("default billing method is required")
    if billing_method.payment_customer_id != payment_customer_id:
        raise InvalidStateTransitionError("billing method does not match subscription")
    instrument = await billing_repository.get_payment_instrument(
        billing_method.instrument_id
    )
    if instrument is None or instrument.status != "active":
        raise InvalidStateTransitionError("active billing key is required")
    if instrument.payment_customer_id != payment_customer_id:
        raise InvalidStateTransitionError("billing key does not match subscription")

    billing_cycle_key = Payment.generate_billing_cycle_key(
        preview.subscription_id,
        now,
    )
    payment = Payment(
        id=Payment.generate_id(),
        order_id=generate_uuid_id("ord"),
        amount=preview.amount,
        status="ready",
        created_at=now,
        subscription_id=preview.subscription_id,
        billing_cycle_key=billing_cycle_key,
        payment_customer_id=payment_customer_id,
        billing_method_id=billing_method.id,
    )
    invoice = Invoice(
        id=Invoice.generate_id(),
        user_id=user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=now,
        subscription_id=preview.subscription_id,
        billing_cycle_key=billing_cycle_key,
    )

    try:
        charged = await provider.charge_billing_key(
            billing_key=billing_key_cipher.decrypt(instrument.billing_key),
            customer_key=payment_customer.customer_key,
            order_id=payment.order_id,
            amount=payment.amount,
            order_name=f"{product_name} plan change",
            idempotency_key=preview.confirmation_token,
        )
    except ProviderError as exc:
        await _raise_failed_plan_change_payment(
            preview=preview,
            payment=payment,
            invoice=invoice,
            message=str(exc),
            provider_code=exc.provider_code,
            retryable=exc.retryable,
        )
    if charged.order_id != payment.order_id or charged.amount != payment.amount:
        await _raise_failed_plan_change_payment(
            preview=preview,
            payment=payment,
            invoice=invoice,
            message="provider billing charge response does not match request",
            provider_code=None,
            retryable=True,
        )

    payment.status = "paid"
    payment.payment_key = charged.payment_key
    payment.approved_at = charged.approved_at
    payment.receipt_url = charged.receipt_url
    payment.method = charged.method
    payment.method_detail = charged.method_detail
    payment.provider_response_summary = charged.response_summary
    payment.cancelable_amount = charged.amount
    invoice.status = "paid"
    invoice.receipt_url = charged.receipt_url

    return PlanUpgradeChargeResult(
        payment=payment,
        invoice=invoice,
        payment_result={
            "invoiceId": invoice.id,
            "paymentId": payment.id,
            "status": payment.status,
            "amount": payment.amount,
            "currency": preview.currency,
            "receiptUrl": payment.receipt_url,
        },
        notification={
            "template": "subscription_plan_upgrade_receipt",
            "queued": True,
        },
    )


async def _raise_failed_plan_change_payment(
    *,
    preview: SubscriptionChangePreview,
    payment: Payment,
    invoice: Invoice,
    message: str,
    provider_code: str | None,
    retryable: bool,
) -> NoReturn:
    payment.status = "failed"
    payment.failure = _plan_change_payment_failure(
        message,
        provider_code=provider_code,
        retryable=retryable,
    )
    result = SubscriptionChangeResult(
        subscription_id=preview.subscription_id,
        product_code=preview.product_code,
        status="active",
        server_decision=preview.server_decision,
        plan_id=preview.current_plan_id,
        previous_plan_id=preview.current_plan_id,
        applied_at=None,
        next_billing_date=preview.next_billing_date,
        payment={
            "invoiceId": invoice.id,
            "paymentId": payment.id,
            "status": payment.status,
            "amount": payment.amount,
            "currency": preview.currency,
            "receiptUrl": None,
            "failure": payment.failure,
        },
        notification=None,
        pending_plan=None,
    )
    raise _PlanUpgradePaymentRequired(
        "subscription plan change payment failed",
        result,
        payment,
        invoice,
    )


def _plan_change_payment_failure(
    message: str,
    *,
    provider_code: str | None,
    retryable: bool,
) -> dict[str, object]:
    return {
        "code": "PLAN_CHANGE_CHARGE_FAILED",
        "providerCode": provider_code or "PROVIDER_BILLING_CHARGE_FAILED",
        "message": message,
        "retryable": retryable,
        "phase": "charge",
        "reason": "provider_rejected" if provider_code else "provider_error",
    }


async def _enqueue_subscription_plan_upgrade_receipt(
    *,
    user_id: str,
    subscription: Subscription,
    from_plan_name: str,
    to_plan_name: str,
    changed_at: datetime,
    currency: str,
    charge_result: PlanUpgradeChargeResult,
    notification_dependencies: NotificationEnqueueDependencies | None,
) -> bool:
    if notification_dependencies is None:
        return True
    payment = charge_result.payment
    invoice = charge_result.invoice
    template_args: TemplateArgs = {
        "subscriptionId": subscription.id,
        "invoiceId": invoice.id,
        "paymentId": payment.id,
        "fromPlanName": from_plan_name,
        "toPlanName": to_plan_name,
        "amount": payment.amount,
        "currency": currency,
        "changedAt": changed_at.isoformat(),
        "receiptUrl": payment.receipt_url or "",
    }
    if subscription.next_billing_at is not None:
        template_args["effectiveAt"] = subscription.next_billing_at.isoformat()
    if payment.method:
        template_args["paymentMethodSummary"] = payment.method
    return await enqueue_user_notification_if_available(
        dependencies=notification_dependencies,
        event_type="subscription_plan_upgrade_receipt",
        recipient_user_id=user_id,
        product_code=subscription.product_code,
        template_args=template_args,
        idempotency_key=(
            "email:subscription_plan_upgrade_receipt:"
            f"{invoice.id}:{payment.id}"
        ),
    )


def _validate_change_preview_still_current(
    *,
    preview: SubscriptionChangePreview,
    subscription: Subscription,
    current_plan: SubscriptionPlan,
    target_plan: SubscriptionPlan,
) -> None:
    expected_decision: Literal["upgrade", "downgrade"] = (
        "upgrade" if target_plan.amount > current_plan.amount else "downgrade"
    )
    expected_amount = _upgrade_proration_amount(
        target_plan.amount - current_plan.amount,
        subscription=subscription,
        calculated_at=preview.created_at,
    )
    expected_next_billing_date = (
        subscription.next_billing_at or subscription.current_period_end_at
    )
    expected_will_apply = (
        "immediate" if expected_decision == "upgrade" else "next_billing_date"
    )
    if (
        preview.server_decision != expected_decision
        or preview.will_apply != expected_will_apply
        or preview.amount != expected_amount
        or preview.currency != target_plan.currency
        or preview.next_billing_date != expected_next_billing_date
    ):
        raise InvalidStateTransitionError("subscription change preview is stale")


def _upgrade_proration_amount(
    price_delta: int,
    *,
    subscription: Subscription,
    calculated_at: datetime,
) -> int:
    if price_delta <= 0:
        return 0
    period_start = subscription.current_period_start_at
    period_end = subscription.next_billing_at or subscription.current_period_end_at
    if period_start is None or period_end is None:
        return price_delta
    total_microseconds = _timedelta_microseconds(period_end - period_start)
    if total_microseconds <= 0:
        return price_delta
    remaining_microseconds = _timedelta_microseconds(period_end - calculated_at)
    remaining_microseconds = max(0, min(remaining_microseconds, total_microseconds))
    if remaining_microseconds == 0:
        return 0
    numerator = price_delta * remaining_microseconds
    return (numerator + total_microseconds - 1) // total_microseconds


def _timedelta_microseconds(delta: timedelta) -> int:
    return (
        (delta.days * 24 * 60 * 60 + delta.seconds) * 1_000_000
        + delta.microseconds
    )


async def _get_owned_subscription(
    subscriptions: SubscriptionAccountRepository,
    subscription_id: str,
    user_id: str,
) -> Subscription:
    subscription = await subscriptions.get_subscription(subscription_id)
    if subscription is None:
        raise ResourceNotFoundError("subscription was not found")
    if subscription.user_id != user_id:
        raise ForbiddenError("subscription belongs to another user")
    return subscription


async def _save_subscription_change_success(
    *,
    subscriptions: SubscriptionAccountRepository,
    billing_repository: BillingRetryRepository,
    idempotency_keys: IdempotencyKeyRepository,
    operator_audits: OperatorAuditRepository | None,
    subscription_change_uow_factory: SubscriptionChangeUnitOfWorkFactory | None,
    subscription: Subscription,
    result: SubscriptionChangeResult,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    user_id: str,
    previous_state: dict[str, object],
    target_plan_id: str,
    payment_result: dict[str, object] | None,
    payment: Payment | None,
    invoice: Invoice | None,
) -> None:
    audit = _subscription_change_audit(
        subscription=subscription,
        result=result,
        processing_key=processing_key,
        request_hash=request_hash,
        now=now,
        user_id=user_id,
        previous_state=previous_state,
        target_plan_id=target_plan_id,
        payment_result=payment_result,
        audit_result="succeeded",
    )
    if subscription_change_uow_factory is None:
        if payment is not None and invoice is not None:
            await billing_repository.save_payment(payment)
            await billing_repository.save_invoice(invoice)
        await subscriptions.save_subscription(subscription)
        await _save_successful_idempotency_response(
            idempotency_keys,
            processing_key,
            request_hash,
            now,
            result,
        )
        if operator_audits is not None:
            await operator_audits.save_operator_audit(audit)
        return

    async with subscription_change_uow_factory() as uow:
        if payment is not None and invoice is not None:
            await uow.billing.save_payment(payment)
            await uow.billing.save_invoice(invoice)
        await uow.subscriptions.save_subscription(subscription)
        await _save_successful_idempotency_response(
            uow.idempotency_keys,
            processing_key,
            request_hash,
            now,
            result,
        )
        await uow.operator_audits.save_operator_audit(audit)


async def _save_subscription_change_failure(
    *,
    billing_repository: BillingRetryRepository,
    idempotency_keys: IdempotencyKeyRepository,
    operator_audits: OperatorAuditRepository | None,
    subscription_change_uow_factory: SubscriptionChangeUnitOfWorkFactory | None,
    result: SubscriptionChangeResult,
    payment: Payment,
    invoice: Invoice,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    user_id: str,
    previous_state: dict[str, object],
    target_plan_id: str,
    payment_result: dict[str, object] | None,
) -> None:
    audit = _subscription_change_audit(
        subscription_id=result.subscription_id,
        result=result,
        processing_key=processing_key,
        request_hash=request_hash,
        now=now,
        user_id=user_id,
        previous_state=previous_state,
        target_plan_id=target_plan_id,
        payment_result=payment_result,
        audit_result="failed",
    )
    if subscription_change_uow_factory is None:
        await billing_repository.save_payment(payment)
        await billing_repository.save_invoice(invoice)
        await _save_failed_idempotency_response(
            idempotency_keys,
            processing_key,
            request_hash,
            now,
            result,
        )
        if operator_audits is not None:
            await operator_audits.save_operator_audit(audit)
        return

    async with subscription_change_uow_factory() as uow:
        await uow.billing.save_payment(payment)
        await uow.billing.save_invoice(invoice)
        await _save_failed_idempotency_response(
            uow.idempotency_keys,
            processing_key,
            request_hash,
            now,
            result,
        )
        await uow.operator_audits.save_operator_audit(audit)


def _subscription_change_audit(
    *,
    subscription: Subscription | None = None,
    subscription_id: str | None = None,
    result: SubscriptionChangeResult,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    user_id: str,
    previous_state: dict[str, object],
    target_plan_id: str,
    payment_result: dict[str, object] | None,
    audit_result: Literal["succeeded", "failed"],
) -> OperatorAudit:
    target_id = subscription.id if subscription is not None else subscription_id
    if target_id is None:
        raise InvalidStateTransitionError("subscription audit target is required")
    return OperatorAudit(
        id=OperatorAudit.generate_id(),
        operator_id=user_id,
        action="subscription.plan_change",
        target_type="subscription",
        target_id=target_id,
        previous_state=previous_state,
        next_state={
            "status": result.status,
            "server_decision": result.server_decision,
            "plan_id": result.plan_id,
            "previous_plan_id": result.previous_plan_id,
            "target_plan_id": target_plan_id,
            "applied_at": result.applied_at.isoformat()
            if result.applied_at is not None
            else None,
            "next_billing_at": result.next_billing_date.isoformat()
            if result.next_billing_date is not None
            else None,
            "pending_plan": result.pending_plan,
            "payment": payment_result,
        },
        reason_code="user_request",
        result=audit_result,
        created_at=now,
        idempotency_key_id=processing_key.id,
        idempotency_scope=processing_key.scope,
        idempotency_key_hash=processing_key.key_hash,
        idempotency_request_hash=request_hash,
    )


def _subscription_change_previous_state(
    subscription: Subscription,
) -> dict[str, object]:
    return {
        "status": subscription.status,
        "plan_id": subscription.plan_id,
        "pending_plan_id": subscription.pending_plan_id,
        "pending_plan_effective_at": (
            subscription.pending_plan_effective_at.isoformat()
            if subscription.pending_plan_effective_at is not None
            else None
        ),
        "next_billing_at": subscription.next_billing_at.isoformat()
        if subscription.next_billing_at is not None
        else None,
    }


async def _save_successful_idempotency_response(
    idempotency_keys: IdempotencyKeyRepository,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    result: SubscriptionChangeResult,
) -> None:
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=processing_key.id,
            scope=SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash,
            request_hash=request_hash,
            status="succeeded",
            created_at=processing_key.created_at,
            updated_at=now,
            expires_at=processing_key.expires_at,
            resource_type=SUBSCRIPTION_CHANGE_CONFIRMATION_RESOURCE,
            resource_id=processing_key.resource_id,
            response_status=200,
            response_body=_change_result_to_response_body(result),
        )
    )


async def _save_failed_idempotency_response(
    idempotency_keys: IdempotencyKeyRepository,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    result: SubscriptionChangeResult,
) -> None:
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=processing_key.id,
            scope=SUBSCRIPTION_CHANGE_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash,
            request_hash=request_hash,
            status="failed",
            created_at=processing_key.created_at,
            updated_at=now,
            expires_at=processing_key.expires_at,
            resource_type=SUBSCRIPTION_CHANGE_CONFIRMATION_RESOURCE,
            resource_id=processing_key.resource_id,
            response_status=402,
            response_body=_change_result_to_response_body(result),
        )
    )


def _preview_result(
    preview: SubscriptionChangePreview,
) -> SubscriptionChangePreviewResult:
    immediate_payment = (
        {
            "amount": preview.amount,
            "currency": preview.currency,
            "invoiceType": "plan_change",
        }
        if preview.server_decision == "upgrade"
        else None
    )
    return SubscriptionChangePreviewResult(
        subscription_id=preview.subscription_id,
        product_code=preview.product_code,
        current_plan_id=preview.current_plan_id,
        target_plan_id=preview.target_plan_id,
        server_decision=preview.server_decision,
        will_apply=preview.will_apply,
        confirmation_token=preview.confirmation_token,
        confirmation_expires_at=preview.expires_at,
        immediate_payment=immediate_payment,
        effective_at=(
            preview.next_billing_date
            if preview.server_decision == "downgrade"
            else None
        ),
        next_billing_date=preview.next_billing_date,
        notice=_notice(preview),
    )


def _preview_result_to_response_body(
    result: SubscriptionChangePreviewResult,
) -> dict[str, object]:
    return {
        "subscriptionId": result.subscription_id,
        "productCode": result.product_code,
        "currentPlanId": result.current_plan_id,
        "targetPlanId": result.target_plan_id,
        "serverDecision": result.server_decision,
        "willApply": result.will_apply,
        "confirmationToken": result.confirmation_token,
        "confirmationExpiresAt": result.confirmation_expires_at,
        "immediatePayment": result.immediate_payment,
        "effectiveAt": result.effective_at,
        "nextBillingDate": result.next_billing_date,
        "notice": result.notice,
    }


def _preview_result_from_response_body(
    body: dict[str, object],
) -> SubscriptionChangePreviewResult:
    server_decision = str(body["serverDecision"])
    if server_decision not in {"upgrade", "downgrade"}:
        raise InvalidStateTransitionError(
            "idempotency response serverDecision is invalid"
        )
    will_apply = str(body["willApply"])
    if will_apply not in {"immediate", "next_billing_date"}:
        raise InvalidStateTransitionError("idempotency response willApply is invalid")
    confirmation_expires_at = body["confirmationExpiresAt"]
    effective_at = body["effectiveAt"]
    next_billing_date = body["nextBillingDate"]
    if not isinstance(confirmation_expires_at, datetime):
        raise InvalidStateTransitionError(
            "idempotency response confirmationExpiresAt is invalid"
        )
    if effective_at is not None and not isinstance(effective_at, datetime):
        raise InvalidStateTransitionError("idempotency response effectiveAt is invalid")
    if next_billing_date is not None and not isinstance(next_billing_date, datetime):
        raise InvalidStateTransitionError(
            "idempotency response nextBillingDate is invalid"
        )
    return SubscriptionChangePreviewResult(
        subscription_id=str(body["subscriptionId"]),
        product_code=str(body["productCode"]),
        current_plan_id=str(body["currentPlanId"]),
        target_plan_id=str(body["targetPlanId"]),
        server_decision="upgrade" if server_decision == "upgrade" else "downgrade",
        will_apply="immediate" if will_apply == "immediate" else "next_billing_date",
        confirmation_token=str(body["confirmationToken"]),
        confirmation_expires_at=confirmation_expires_at,
        immediate_payment=_dict_or_none(body["immediatePayment"]),
        effective_at=effective_at,
        next_billing_date=next_billing_date,
        notice=str(body["notice"]),
    )


def _notice(preview: SubscriptionChangePreview) -> str:
    if preview.server_decision == "upgrade":
        next_billing_date = _format_date(preview.next_billing_date)
        return (
            f"업그레이드는 확인 즉시 {_format_krw(preview.amount)}이 결제되고 "
            f"플랜이 바로 변경됩니다. 다음 결제일은 {next_billing_date}입니다."
        )
    return (
        f"다운그레이드는 다음 결제일인 "
        f"{_format_date(preview.next_billing_date)}에 변경됩니다. "
        "현재 결제 기간에는 기존 플랜 권한이 유지됩니다."
    )


def _format_krw(amount: int) -> str:
    return f"{amount:,}원"


def _format_date(value: datetime | None) -> str:
    return value.date().isoformat() if value is not None else "다음 결제일"


def _plan_name(product_name: str, billing_period: str) -> str:
    period_label = "monthly" if billing_period == "monthly" else "yearly"
    return f"{product_name} {period_label}"


def _change_result_to_response_body(
    result: SubscriptionChangeResult,
) -> dict[str, object]:
    return {
        "subscriptionId": result.subscription_id,
        "productCode": result.product_code,
        "status": result.status,
        "serverDecision": result.server_decision,
        "planId": result.plan_id,
        "previousPlanId": result.previous_plan_id,
        "appliedAt": result.applied_at,
        "nextBillingDate": result.next_billing_date,
        "payment": result.payment,
        "notification": result.notification,
        "pendingPlan": result.pending_plan,
    }


def _change_result_from_response_body(
    body: dict[str, object],
) -> SubscriptionChangeResult:
    applied_at = body["appliedAt"]
    next_billing_date = body["nextBillingDate"]
    if applied_at is not None and not isinstance(applied_at, datetime):
        raise InvalidStateTransitionError("idempotency response appliedAt is invalid")
    if next_billing_date is not None and not isinstance(next_billing_date, datetime):
        raise InvalidStateTransitionError(
            "idempotency response nextBillingDate is invalid"
        )
    server_decision = str(body["serverDecision"])
    if server_decision not in {"upgrade", "downgrade"}:
        raise InvalidStateTransitionError(
            "idempotency response serverDecision is invalid"
        )
    server_decision_value: Literal["upgrade", "downgrade"] = (
        "upgrade" if server_decision == "upgrade" else "downgrade"
    )
    return SubscriptionChangeResult(
        subscription_id=str(body["subscriptionId"]),
        product_code=str(body["productCode"]),
        status=str(body["status"]),
        server_decision=server_decision_value,
        plan_id=str(body["planId"]),
        previous_plan_id=str(body["previousPlanId"]),
        applied_at=applied_at,
        next_billing_date=next_billing_date,
        payment=_dict_or_none(body["payment"]),
        notification=_dict_or_none(body["notification"]),
        pending_plan=_dict_or_none(body["pendingPlan"]),
    )


def _change_result_from_existing_idempotency_key(
    key: IdempotencyKey,
) -> SubscriptionChangeResult:
    if key.status == "processing":
        raise InvalidStateTransitionError("subscription change is processing")
    if key.response_body is None:
        raise InvalidStateTransitionError("subscription change result is unavailable")
    result = _change_result_from_response_body(key.response_body)
    if key.response_status == 402:
        raise PaymentRequiredResponseError(
            "subscription plan change payment failed",
            key.response_body,
        )
    return result


def _dict_or_none(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidStateTransitionError("idempotency response object is invalid")
    return value


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
