from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    BadRequestError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentConfirmRejectedError,
    ProviderError,
    ResourceNotFoundError,
)
from payments.application.operation_locks import (
    acquire_required_operation_lock,
    release_operation_lock,
)
from payments.application.ports import (
    CheckoutRepository,
    Clock,
    OneTimePaymentUnitOfWorkFactory,
    OneTimeSkuRepository,
    OperationLockRepository,
    PaymentAttemptRepository,
    PaymentCancelProviderResult,
    PaymentCustomerRepository,
    PaymentProvider,
)
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_cancel_request import PaymentCancelRequest
from payments.domain.entities.payment_customer import PaymentCustomer

IDEMPOTENCY_SCOPE = "payments-orders"
PAYMENT_CONFIRM_IDEMPOTENCY_SCOPE = "payments-confirm"
PAYMENT_CANCEL_IDEMPOTENCY_SCOPE = "payments-cancel"
PAYMENT_AUTH_RESULT_IDEMPOTENCY_SCOPE = "payments-auth-result"


@dataclass(frozen=True, slots=True)
class PaymentOrderItem:
    sku_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class PaymentOrderResult:
    checkout_id: str
    payment_id: str
    order_id: str
    attempt_no: int
    order_name: str
    amount: int
    currency: str
    customer_key: str
    client_key: str
    success_url: str
    fail_url: str
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PricedPaymentOrderItem:
    sku_id: str
    quantity: int
    unit_amount: int
    amount: int
    purchase_limit: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PaymentOrderPricing:
    items: list[PricedPaymentOrderItem]
    amount: int


@dataclass(frozen=True, slots=True)
class PaymentDetail:
    checkout_id: str
    payment_id: str
    order_id: str
    attempt_no: int
    status: str
    amount: int
    currency: str
    order_name: str
    approved_at: datetime | None
    receipt_url: str | None
    method: str | None
    method_detail: dict[str, object] | None
    failure: dict[str, object] | None
    retry: dict[str, object]


@dataclass(frozen=True, slots=True)
class PaymentAuthFailureCommand:
    order_id: str
    code: str
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentAuthFailureResult:
    checkout_id: str
    payment_id: str
    order_id: str
    status: str
    failure: dict[str, object]
    retry: dict[str, object]


@dataclass(frozen=True, slots=True)
class PaymentConfirmCommand:
    payment_id: str
    payment_key: str
    order_id: str
    amount: int


@dataclass(frozen=True, slots=True)
class PaymentConfirmResult:
    checkout_id: str
    payment_id: str
    order_id: str
    attempt_no: int
    payment_key: str
    status: str
    amount: int
    currency: str
    approved_at: datetime
    receipt_url: str | None
    method: str


@dataclass(frozen=True, slots=True)
class PaymentCancelCommand:
    cancel_amount: int | None
    cancel_reason: str
    reason_message: str | None = None
    refund_bank_account: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PaymentCancelResult:
    payment_id: str
    payment_key: str
    status: str
    paid_amount: int
    canceled_amount: int
    cancelable_amount: int
    latest_cancel: dict[str, object]
    cancel_history: list[dict[str, object]]


async def create_payment_order(
    requester: RequestContext,
    items: list[PaymentOrderItem],
    success_url: str,
    fail_url: str,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    clock: Clock,
    client_key: str = "test_ck_local",
    idempotency_key: str | None = None,
    checkout_id: str | None = None,
) -> PaymentOrderResult:
    """일반결제 주문과 결제 시도를 생성합니다.

    Args:
        requester: 내부 백엔드가 인증해 전달한 요청 컨텍스트입니다.
        items: 구매하려는 one-time SKU와 수량 목록입니다.
        success_url: 결제 인증 성공 후 돌아갈 URL입니다.
        fail_url: 결제 인증 실패 후 돌아갈 URL입니다.
        one_time_payment_uow_factory: 일반결제 주문 생성을 위한 UoW factory입니다.
        clock: 생성 시각과 만료 시각을 결정하는 시간 포트입니다.
        client_key: 프론트가 Toss SDK에 전달할 클라이언트 키입니다.
        idempotency_key: 같은 주문 생성 요청 중복 실행을 막는 선택 키입니다.
        checkout_id: 실패 또는 취소 후 재시도할 기존 체크아웃 ID입니다.

    Returns:
        결제창 호출에 필요한 주문 생성 결과입니다.

    Raises:
        AuthorizationError: 요청 사용자 ID가 없는 경우 발생합니다.
        ResourceNotFoundError: 재시도 체크아웃이 요청자 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 체크아웃 상태가 재시도 불가면 발생합니다.
        IdempotencyConflictError: 같은 멱등성 키가 다른 payload에 쓰인 경우입니다.
    """
    user_id = _require_user_id(requester)
    _validate_items(items)
    payload = {
        "items": [{"skuId": item.sku_id, "quantity": item.quantity} for item in items],
        "successUrl": success_url,
        "failUrl": fail_url,
        "checkoutId": checkout_id,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    now = clock.utc_now()

    async with one_time_payment_uow_factory() as uow:
        if key_hash:
            existing = await uow.idempotency_keys.find_idempotency_key(
                IDEMPOTENCY_SCOPE, key_hash
            )
            if existing and existing.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was used with another payload"
                )
            if existing and existing.response_body:
                return PaymentOrderResult(
                    checkout_id=existing.response_body["checkoutId"],
                    payment_id=existing.response_body["paymentId"],
                    order_id=existing.response_body["orderId"],
                    attempt_no=existing.response_body["attemptNo"],
                    order_name=existing.response_body["orderName"],
                    amount=existing.response_body["amount"],
                    currency=existing.response_body["currency"],
                    customer_key=existing.response_body["customerKey"],
                    client_key=existing.response_body["clientKey"],
                    success_url=existing.response_body["successUrl"],
                    fail_url=existing.response_body["failUrl"],
                    status=existing.response_body["status"],
                    expires_at=datetime.fromisoformat(
                        existing.response_body["expiresAt"]
                    ),
                )

        payment_customer = await _get_or_create_payment_customer(
            uow.payment_customers,
            user_id,
        )
        pricing = await _price_order_items(uow.one_time_skus, items)
        await _validate_order_purchase_limits(
            user_id=user_id,
            items=pricing.items,
            payments=uow.payments,
        )
        checkout = await _resolve_checkout(
            user_id=user_id,
            checkout_id=checkout_id,
            items=pricing.items,
            payment_customer_id=payment_customer.id,
            checkouts=uow.checkouts,
            now=now,
        )
        await _reserve_priced_order_items(uow.one_time_skus, pricing.items)
        checkout.status = "ready"
        payment_id = Payment.generate_id()
        expires_at = now + timedelta(minutes=30)
        payment = Payment(
            id=payment_id,
            order_id=f"order_{payment_id}",
            amount=pricing.amount,
            status="ready",
            created_at=now,
            checkout_id=checkout.id,
            payment_customer_id=checkout.payment_customer_id,
            expires_at=expires_at,
        )
        checkout.last_payment_id = payment.id
        attempt_no = await uow.payments.count_payments_for_checkout(checkout.id) + 1

        result = PaymentOrderResult(
            checkout_id=checkout.id,
            payment_id=payment.id,
            order_id=payment.order_id,
            attempt_no=attempt_no,
            order_name=_order_name(pricing.items),
            amount=payment.amount,
            currency=_currency(pricing.items),
            customer_key=payment_customer.customer_key,
            client_key=client_key,
            success_url=_append_payment_id(success_url, payment.id),
            fail_url=_append_payment_id(fail_url, payment.id),
            status=payment.status,
            expires_at=expires_at,
        )

        await uow.checkouts.save_checkout(checkout)
        await uow.payments.save_payment(payment)

        if key_hash:
            await uow.idempotency_keys.save_idempotency_key(
                IdempotencyKey(
                    id=IdempotencyKey.generate_id(),
                    scope=IDEMPOTENCY_SCOPE,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    status="succeeded",
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(hours=24),
                    resource_type="payment",
                    resource_id=payment.id,
                    response_status=200,
                    response_body=_result_to_response_body(result),
                )
            )

        return result


async def get_payment_detail(
    requester: RequestContext,
    payment_id: str,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    clock: Clock,
) -> PaymentDetail:
    """요청 회원이 소유한 결제 상세를 조회합니다.

    Args:
        requester: 내부 백엔드가 인증해 전달한 요청 컨텍스트입니다.
        payment_id: 조회할 결제 ID입니다.
        one_time_payment_uow_factory: 조회 중 lazy-expire와 재고 해제를
            처리하는 UoW입니다.
        clock: 만료 판정에 사용할 시간 포트입니다.

    Returns:
        결제 상세 정보입니다.

    Raises:
        AuthorizationError: 요청 사용자 ID가 없는 경우 발생합니다.
        ForbiddenError: 결제가 있지만 요청자 소유가 아닌 경우 발생합니다.
        ResourceNotFoundError: 결제가 없는 경우 발생합니다.
    """
    user_id = _require_user_id(requester)
    async with one_time_payment_uow_factory() as uow:
        payment = await uow.payments.get_payment_for_user(payment_id, user_id)
        if payment is None or payment.checkout_id is None:
            existing_payment = await uow.payments.get_payment(payment_id)
            if existing_payment is None or existing_payment.checkout_id is None:
                raise ResourceNotFoundError("payment not found")
            raise ForbiddenError("payment is not owned by requester")
        checkout = await uow.checkouts.get_checkout_for_user(
            payment.checkout_id,
            user_id,
        )
        if checkout is None:
            raise ResourceNotFoundError("checkout not found")

        if (
            payment.status == "ready"
            and payment.payment_key is None
            and payment.expires_at is not None
            and payment.expires_at <= clock.utc_now()
        ):
            payment.status = "expired"
            payment.failure = {
                "phase": "before_confirm",
                "reason": "auth_result_not_reported",
                "retryable": True,
            }
            checkout.status = "expired"
            await _release_checkout_reserved_stock(checkout, uow.one_time_skus)
            await uow.payments.save_payment(payment)
            await uow.checkouts.save_checkout(checkout)

        attempt_no = await uow.payments.get_payment_attempt_no(
            checkout.id,
            payment.id,
        )
        return _payment_detail(payment, checkout, attempt_no)


async def cancel_payment(
    requester: RequestContext,
    payment_id: str,
    command: PaymentCancelCommand,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    provider: PaymentProvider,
    clock: Clock,
    idempotency_key: str,
    operation_locks: OperationLockRepository | None = None,
) -> PaymentCancelResult:
    """승인 완료된 일반결제를 전체 또는 부분 취소합니다.

    Args:
        requester: 내부 백엔드가 인증해 전달한 요청 컨텍스트입니다.
        payment_id: 취소할 결제 ID입니다.
        command: 취소 금액과 사유입니다.
        one_time_payment_uow_factory: 결제 상태를 저장하는 UoW입니다.
        provider: 결제 취소 provider 포트입니다.
        clock: 취소 시각 fallback을 제공하는 시간 포트입니다.
        idempotency_key: 취소 중복 클릭과 재시도를 한 번으로 묶는 필수 키입니다.
        operation_locks: paymentId 단위 취소 동시 실행을 막는 lock 저장소입니다.

    Returns:
        취소 후 결제 상태와 취소 이력입니다.

    Raises:
        AuthorizationError: 요청 사용자 ID가 없는 경우 발생합니다.
        ResourceNotFoundError: 결제가 없거나 요청자 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 취소할 수 없는 상태 또는 금액입니다.
        IdempotencyConflictError: 같은 멱등성 키가 다른 payload에 쓰인 경우입니다.
    """
    user_id = _require_user_id(requester)
    payload = {
        "paymentId": payment_id,
        "cancelAmount": command.cancel_amount,
        "cancelReason": command.cancel_reason,
        "reasonMessage": command.reason_message,
        "refundBankAccount": command.refund_bank_account,
    }
    if not command.cancel_reason.strip():
        raise BadRequestError("cancel reason is required")
    request_hash = _hash_payload(payload)
    cancel_request_key_hash = _hash_text(idempotency_key)
    key_hash = _hash_text(f"{payment_id}:{idempotency_key}")
    now = clock.utc_now()
    processing_key: IdempotencyKey | None = None
    pending_cancel_request: PaymentCancelRequest | None = None
    payment_key: str | None = None
    cancel_amount: int | None = None
    paid_amount: int | None = None
    expected_cancelable_amount: int | None = None
    audit_id: str | None = None
    existing_provider_cancel_ids: set[str] = set()
    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"payment-cancel:{payment_id}",
        fencing_counter_key="payment-cancel",
        now=now,
        metadata={
            "paymentId": payment_id,
            "requestId": requester.request_id,
            "userId": user_id,
            "idempotencyKeyHash": cancel_request_key_hash,
        },
    )

    try:
        async with one_time_payment_uow_factory() as uow:
            payment = await uow.payments.get_payment_for_user(payment_id, user_id)
            if payment is None:
                if await uow.payments.get_payment(payment_id) is None:
                    raise ResourceNotFoundError("payment not found")
                raise ForbiddenError("payment is not owned by requester")
            existing_key = await uow.idempotency_keys.find_idempotency_key(
                PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                key_hash,
            )
            if existing_key is not None and existing_key.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was used with another payload"
                )
            if existing_key is not None and existing_key.response_body is not None:
                return _payment_cancel_result_from_response_body(
                    existing_key.response_body
                )
            if existing_key is not None and existing_key.status == "processing":
                raise InvalidStateTransitionError("payment cancellation is processing")
            if payment.status not in {"paid", "partial_canceled"}:
                raise InvalidStateTransitionError("payment cannot be canceled")
            if not payment.payment_key:
                raise InvalidStateTransitionError("payment key is required")
            cancelable_amount = _cancelable_amount(payment)
            cancel_amount = (
                cancelable_amount
                if command.cancel_amount is None
                else command.cancel_amount
            )
            if cancel_amount < 1 or cancel_amount > cancelable_amount:
                raise BadRequestError("cancel amount is invalid")

            existing_cancel_request = (
                await uow.payment_cancel_requests.find_payment_cancel_request(
                    payment_id,
                    cancel_request_key_hash,
                )
            )
            if (
                existing_cancel_request is not None
                and existing_cancel_request.status == "pending"
            ):
                raise InvalidStateTransitionError("payment cancellation is processing")
            audit_id = (
                existing_cancel_request.operator_audit_id
                if existing_cancel_request is not None
                and existing_cancel_request.operator_audit_id is not None
                else OperatorAudit.generate_id()
            )
            pending_cancel_request = PaymentCancelRequest(
                id=(
                    existing_cancel_request.id
                    if existing_cancel_request is not None
                    else PaymentCancelRequest.generate_id()
                ),
                payment_id=payment.id,
                idempotency_key_hash=cancel_request_key_hash,
                status="pending",
                cancel_amount=cancel_amount,
                cancel_reason=command.cancel_reason,
                requested_by="user",
                requested_user_id=user_id,
                operator_audit_id=audit_id,
                created_at=(
                    existing_cancel_request.created_at
                    if existing_cancel_request is not None
                    else now
                ),
                updated_at=now,
            )
            processing_key = IdempotencyKey(
                id=(
                    existing_key.id
                    if existing_key is not None
                    else IdempotencyKey.generate_id()
                ),
                scope=PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="processing",
                created_at=(
                    existing_key.created_at if existing_key is not None else now
                ),
                updated_at=now,
                expires_at=now + timedelta(hours=24),
                resource_type="payment_cancel_request",
                resource_id=pending_cancel_request.id,
                locked_until_at=now + timedelta(minutes=5),
            )
            payment_key = payment.payment_key
            paid_amount = payment.amount
            expected_cancelable_amount = cancelable_amount - cancel_amount
            existing_provider_cancel_ids = _provider_cancel_ids(payment.cancel_history)
            await uow.payment_cancel_requests.save_payment_cancel_request(
                pending_cancel_request
            )
            await uow.idempotency_keys.save_idempotency_key(processing_key)

        if (
            processing_key is None
            or pending_cancel_request is None
            or payment_key is None
            or cancel_amount is None
            or paid_amount is None
            or expected_cancelable_amount is None
            or audit_id is None
        ):
            raise InvalidStateTransitionError("payment cancellation was not prepared")

        try:
            provider_result = await provider.cancel_payment(
                payment_key=payment_key,
                cancel_amount=cancel_amount,
                cancel_reason=command.cancel_reason,
                refund_bank_account=command.refund_bank_account,
                idempotency_key=idempotency_key,
            )
        except ProviderError as exc:
            await _mark_payment_cancel_request_failed(
                one_time_payment_uow_factory,
                processing_key,
                pending_cancel_request,
                request_hash,
                clock.utc_now(),
                "provider cancel failed",
                provider_code=exc.provider_code,
                retryable=exc.retryable,
            )
            raise
        if _provider_cancel_response_mismatches(
            provider_result=provider_result,
            cancel_amount=cancel_amount,
            paid_amount=paid_amount,
            expected_cancelable_amount=expected_cancelable_amount,
        ):
            await _mark_payment_cancel_request_failed(
                one_time_payment_uow_factory,
                processing_key,
                pending_cancel_request,
                request_hash,
                clock.utc_now(),
                "provider response does not match",
                provider_code=None,
                retryable=True,
            )
            raise ProviderError("provider response does not match")
        if provider_result.cancel_id in existing_provider_cancel_ids:
            await _mark_payment_cancel_request_failed(
                one_time_payment_uow_factory,
                processing_key,
                pending_cancel_request,
                request_hash,
                clock.utc_now(),
                "provider cancel id is duplicated",
                provider_code=None,
                retryable=True,
            )
            raise ProviderError("provider cancel id is duplicated")

        async with one_time_payment_uow_factory() as uow:
            payment = await uow.payments.get_payment_for_user(payment_id, user_id)
            if payment is None:
                if await uow.payments.get_payment(payment_id) is None:
                    raise ResourceNotFoundError("payment not found")
                raise ForbiddenError("payment is not owned by requester")
            if payment.status not in {"paid", "partial_canceled"}:
                raise InvalidStateTransitionError("payment cannot be canceled")
            if payment.payment_key != payment_key:
                raise InvalidStateTransitionError("payment key does not match")
            cancelable_amount = _cancelable_amount(payment)
            if cancel_amount < 1 or cancel_amount > cancelable_amount:
                raise BadRequestError("cancel amount is invalid")
            previous_state = _payment_cancel_audit_state(payment)
            latest_cancel = {
                "cancelId": pending_cancel_request.id,
                "providerCancelId": provider_result.cancel_id,
                "cancelAmount": provider_result.cancel_amount,
                "cancelReason": command.cancel_reason,
                "reasonMessage": command.reason_message,
                "canceledAt": provider_result.canceled_at,
                "receiptUrl": provider_result.receipt_url,
                "requestedBy": "user",
                "status": "succeeded",
            }
            cancel_history = [*(payment.cancel_history or []), latest_cancel]
            payment.cancel_history = cancel_history
            payment.cancelable_amount = max(cancelable_amount - cancel_amount, 0)
            payment.status = (
                "canceled" if payment.cancelable_amount == 0 else "partial_canceled"
            )
            if payment.status == "canceled" and payment.checkout_id is not None:
                checkout = await uow.checkouts.get_checkout_for_user(
                    payment.checkout_id,
                    user_id,
                )
                if checkout is not None:
                    await _restore_checkout_sold_stock(checkout, uow.one_time_skus)
            await uow.payments.save_payment(payment)
            succeeded_cancel_request = PaymentCancelRequest(
                id=pending_cancel_request.id,
                payment_id=pending_cancel_request.payment_id,
                idempotency_key_hash=pending_cancel_request.idempotency_key_hash,
                status="succeeded",
                cancel_amount=pending_cancel_request.cancel_amount,
                cancel_reason=pending_cancel_request.cancel_reason,
                requested_by=pending_cancel_request.requested_by,
                requested_user_id=pending_cancel_request.requested_user_id,
                operator_audit_id=pending_cancel_request.operator_audit_id,
                provider_cancel_id=provider_result.cancel_id,
                canceled_at=provider_result.canceled_at,
                receipt_url=provider_result.receipt_url,
                created_at=pending_cancel_request.created_at,
                updated_at=clock.utc_now(),
            )
            await uow.payment_cancel_requests.save_payment_cancel_request(
                succeeded_cancel_request
            )
            await uow.operator_audits.save_operator_audit(
                OperatorAudit(
                    id=audit_id,
                    operator_id=user_id,
                    action="payment.cancel",
                    target_type="payment",
                    target_id=payment.id,
                    previous_state=previous_state,
                    next_state={
                        **_payment_cancel_audit_state(payment),
                        "cancel_amount": cancel_amount,
                        "cancel_reason": command.cancel_reason,
                        "reason_message": command.reason_message,
                        "requested_by": "user",
                        "notification": _payment_cancel_notification(
                            cancel_amount=cancel_amount,
                        ),
                    },
                    reason_code=command.cancel_reason,
                    result="succeeded",
                    created_at=clock.utc_now(),
                    idempotency_key_id=processing_key.id,
                    idempotency_scope=PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                    idempotency_key_hash=key_hash,
                    idempotency_request_hash=request_hash,
                    reason_message=command.reason_message,
                )
            )
            result = PaymentCancelResult(
                payment_id=payment.id,
                payment_key=payment_key,
                status=payment.status,
                paid_amount=payment.amount,
                canceled_amount=sum(
                    int(cancel.get("cancelAmount", 0)) for cancel in cancel_history
                ),
                cancelable_amount=payment.cancelable_amount,
                latest_cancel=latest_cancel,
                cancel_history=cancel_history,
            )
            await uow.idempotency_keys.save_idempotency_key(
                _succeeded_idempotency_key(
                    existing_key=processing_key,
                    scope=PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    now=clock.utc_now(),
                    resource_type="payment_cancel_request",
                    resource_id=succeeded_cancel_request.id,
                    response_body=_cancel_result_to_response_body(result),
                )
            )
            return result
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def confirm_payment(
    requester: RequestContext,
    command: PaymentConfirmCommand,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    provider: PaymentProvider,
    clock: Clock,
    idempotency_key: str,
    operation_locks: OperationLockRepository | None = None,
) -> PaymentConfirmResult:
    """토스 결제창 인증 성공 후 일반결제를 최종 승인합니다.

    Args:
        requester: 내부 백엔드가 인증해 전달한 요청 컨텍스트입니다.
        command: 프론트 성공 페이지가 전달한 paymentKey, orderId, amount입니다.
        one_time_payment_uow_factory: 결제/체크아웃/재고를 함께 갱신하는 UoW입니다.
        provider: 결제 승인 provider 포트입니다.
        clock: 만료 판정에 사용할 시간 포트입니다.
        idempotency_key: 성공 페이지 재진입과 네트워크 재시도를 묶는 필수 키입니다.
        operation_locks: paymentId와 checkoutId 단위 승인 동시 실행을 막는
            lock 저장소입니다.

    Returns:
        승인된 결제 결과입니다.

    Raises:
        AuthorizationError: 요청 사용자 ID가 없는 경우 발생합니다.
        ResourceNotFoundError: 결제가 없거나 요청자 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 주문/금액 불일치 또는 확정 불가 상태입니다.
    """
    user_id = _require_user_id(requester)
    payload = {
        "paymentId": command.payment_id,
        "paymentKey": command.payment_key,
        "orderId": command.order_id,
        "amount": command.amount,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key)
    now = clock.utc_now()
    processing_key: IdempotencyKey | None = None
    checkout_operation_lock = None
    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"payment-confirm:{command.payment_id}",
        fencing_counter_key="payment-confirm",
        now=now,
        metadata={
            "paymentId": command.payment_id,
            "requestId": requester.request_id,
            "userId": user_id,
            "idempotencyKeyHash": key_hash,
        },
    )
    try:
        async with one_time_payment_uow_factory() as uow:
            payment = await uow.payments.get_payment_for_user(
                command.payment_id,
                user_id,
            )
            if payment is None or payment.checkout_id is None:
                raise ResourceNotFoundError("payment not found")
            checkout = await uow.checkouts.get_checkout_for_user(
                payment.checkout_id,
                user_id,
            )
            if checkout is None:
                raise ResourceNotFoundError("checkout not found")
            checkout_operation_lock = await acquire_required_operation_lock(
                operation_locks=operation_locks,
                lock_key=f"checkout-confirm:{checkout.id}",
                fencing_counter_key="checkout-confirm",
                now=now,
                metadata={
                    "paymentId": command.payment_id,
                    "checkoutId": checkout.id,
                    "requestId": requester.request_id,
                    "userId": user_id,
                    "idempotencyKeyHash": key_hash,
                },
            )
            existing_key = await uow.idempotency_keys.find_idempotency_key(
                PAYMENT_CONFIRM_IDEMPOTENCY_SCOPE,
                key_hash,
            )
            if existing_key is not None and existing_key.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was used with another payload"
                )
            if existing_key is not None and existing_key.response_body is not None:
                if existing_key.response_status == 402:
                    raise PaymentConfirmRejectedError(existing_key.response_body)
                return _payment_confirm_result_from_response_body(
                    existing_key.response_body
                )
            if existing_key is not None and existing_key.status == "processing":
                raise InvalidStateTransitionError(
                    "payment confirmation is processing"
                )
            if payment.status == "paid" and payment.payment_key == command.payment_key:
                if payment.approved_at is None:
                    raise InvalidStateTransitionError(
                        "paid payment has no approved time"
                    )
                result = _payment_confirm_result(
                    payment,
                    await uow.payments.get_payment_attempt_no(
                        checkout.id,
                        payment.id,
                    ),
                )
                await uow.idempotency_keys.save_idempotency_key(
                    _succeeded_idempotency_key(
                        existing_key=existing_key,
                        scope=PAYMENT_CONFIRM_IDEMPOTENCY_SCOPE,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        now=now,
                        resource_type="payment",
                        resource_id=payment.id,
                        response_body=_confirm_result_to_response_body(result),
                    )
                )
                return result
            if checkout.status != "ready":
                raise InvalidStateTransitionError("checkout cannot be confirmed")
            if payment.status != "ready":
                raise InvalidStateTransitionError("payment cannot be confirmed")
            if payment.expires_at is not None and payment.expires_at <= now:
                payment.status = "expired"
                payment.failure = {
                    "phase": "before_confirm",
                    "reason": "auth_result_not_reported",
                    "retryable": True,
                }
                checkout.status = "expired"
                await _release_checkout_reserved_stock(checkout, uow.one_time_skus)
                await uow.payments.save_payment(payment)
                await uow.checkouts.save_checkout(checkout)
                raise InvalidStateTransitionError("payment is expired")
            if payment.order_id != command.order_id:
                await _mark_payment_confirm_validation_failed(
                    payment=payment,
                    checkout=checkout,
                    one_time_skus=uow.one_time_skus,
                    payments=uow.payments,
                    checkouts=uow.checkouts,
                    message="order id does not match payment",
                )
                raise BadRequestError("order id does not match payment")
            if payment.amount != command.amount:
                await _mark_payment_confirm_validation_failed(
                    payment=payment,
                    checkout=checkout,
                    one_time_skus=uow.one_time_skus,
                    payments=uow.payments,
                    checkouts=uow.checkouts,
                    message="amount does not match payment",
                )
                raise BadRequestError("amount does not match payment")

            processing_key = IdempotencyKey(
                id=(
                    existing_key.id
                    if existing_key is not None
                    else IdempotencyKey.generate_id()
                ),
                scope=PAYMENT_CONFIRM_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="processing",
                created_at=(
                    existing_key.created_at if existing_key is not None else now
                ),
                updated_at=now,
                expires_at=now + timedelta(hours=24),
                resource_type="payment",
                resource_id=payment.id,
                locked_until_at=now + timedelta(minutes=5),
            )
            await uow.idempotency_keys.save_idempotency_key(processing_key)

        if processing_key is None:
            raise InvalidStateTransitionError("payment confirmation was not prepared")

        try:
            provider_result = await provider.confirm_payment(
                payment_key=command.payment_key,
                order_id=command.order_id,
                amount=command.amount,
                idempotency_key=idempotency_key,
            )
        except ProviderError as exc:
            await _mark_payment_confirm_failed(
                requester_user_id=user_id,
                command=command,
                one_time_payment_uow_factory=one_time_payment_uow_factory,
                processing_key=processing_key,
                key_hash=key_hash,
                request_hash=request_hash,
                clock=clock,
                message=str(exc),
                provider_code=exc.provider_code,
                retryable=exc.retryable,
            )
        if (
            provider_result.payment_key != command.payment_key
            or provider_result.order_id != command.order_id
            or provider_result.amount != command.amount
        ):
            await _mark_payment_confirm_failed(
                requester_user_id=user_id,
                command=command,
                one_time_payment_uow_factory=one_time_payment_uow_factory,
                processing_key=processing_key,
                key_hash=key_hash,
                request_hash=request_hash,
                clock=clock,
                message="provider response does not match",
                provider_code=None,
                retryable=True,
            )

        async with one_time_payment_uow_factory() as uow:
            payment = await uow.payments.get_payment_for_user(
                command.payment_id,
                user_id,
            )
            if payment is None or payment.checkout_id is None:
                raise ResourceNotFoundError("payment not found")
            checkout = await uow.checkouts.get_checkout_for_user(
                payment.checkout_id,
                user_id,
            )
            if checkout is None:
                raise ResourceNotFoundError("checkout not found")
            if payment.status == "paid" and payment.payment_key == command.payment_key:
                result = _payment_confirm_result(
                    payment,
                    await uow.payments.get_payment_attempt_no(
                        checkout.id,
                        payment.id,
                    ),
                )
                await uow.idempotency_keys.save_idempotency_key(
                    _succeeded_idempotency_key(
                        existing_key=processing_key,
                        scope=PAYMENT_CONFIRM_IDEMPOTENCY_SCOPE,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        now=clock.utc_now(),
                        resource_type="payment",
                        resource_id=payment.id,
                        response_body=_confirm_result_to_response_body(result),
                    )
                )
                return result
            if payment.status != "ready":
                raise InvalidStateTransitionError("payment cannot be confirmed")
            payment.status = "paid"
            payment.payment_key = provider_result.payment_key
            payment.approved_at = provider_result.approved_at
            payment.receipt_url = provider_result.receipt_url
            payment.method = provider_result.method
            payment.method_detail = provider_result.method_detail
            payment.provider_response_summary = provider_result.response_summary
            payment.cancelable_amount = payment.amount
            checkout_marked_paid = await uow.checkouts.mark_checkout_paid_if_ready(
                checkout.id,
                user_id,
                payment.id,
            )
            if not checkout_marked_paid:
                raise InvalidStateTransitionError("checkout cannot be marked paid")
            checkout.status = "paid"
            checkout.last_payment_id = payment.id
            invoice = Invoice(
                id=Invoice.generate_id(),
                user_id=user_id,
                payment_id=payment.id,
                status="paid",
                issued_at=provider_result.approved_at,
                receipt_url=provider_result.receipt_url,
            )
            await _capture_checkout_reserved_stock(checkout, uow.one_time_skus)
            await uow.payments.save_payment(payment)
            await uow.invoices.save_invoice(invoice)
            result = _payment_confirm_result(
                payment,
                await uow.payments.get_payment_attempt_no(checkout.id, payment.id),
            )
            await uow.idempotency_keys.save_idempotency_key(
                _succeeded_idempotency_key(
                    existing_key=processing_key,
                    scope=PAYMENT_CONFIRM_IDEMPOTENCY_SCOPE,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    now=clock.utc_now(),
                    resource_type="payment",
                    resource_id=payment.id,
                    response_body=_confirm_result_to_response_body(result),
                )
            )
            return result
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=checkout_operation_lock,
            released_at=clock.utc_now(),
        )
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def record_payment_auth_failure(
    requester: RequestContext,
    payment_id: str,
    command: PaymentAuthFailureCommand,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    clock: Clock,
    idempotency_key: str | None = None,
) -> PaymentAuthFailureResult:
    """토스 결제창 승인 전 실패 또는 사용자 취소를 기록합니다.

    Args:
        requester: 내부 백엔드가 인증해 전달한 요청 컨텍스트입니다.
        payment_id: 실패 결과를 기록할 결제 시도 ID입니다.
        command: failUrl에서 받은 orderId, provider code, 메시지입니다.
        one_time_payment_uow_factory: 결제/체크아웃/재고를 함께 갱신하는 UoW입니다.
        clock: 만료 판정에 사용할 시간 포트입니다.
        idempotency_key: 실패 페이지 새로고침을 같은 결과로 묶는 선택 키입니다.

    Returns:
        실패 기록과 재시도 안내입니다.

    Raises:
        AuthorizationError: 요청 사용자 ID가 없는 경우 발생합니다.
        ResourceNotFoundError: 결제가 없거나 요청자 소유가 아닌 경우 발생합니다.
        BadRequestError: orderId가 내부 결제 시도와 일치하지 않는 경우입니다.
        InvalidStateTransitionError: 이미 확정된 결제인 경우입니다.
        IdempotencyConflictError: 같은 멱등성 키가 다른 payload에 쓰인 경우입니다.
    """
    user_id = _require_user_id(requester)
    payload = {
        "paymentId": payment_id,
        "orderId": command.order_id,
        "code": command.code,
        "message": command.message,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    now = clock.utc_now()
    async with one_time_payment_uow_factory() as uow:
        payment = await uow.payments.get_payment_for_user(payment_id, user_id)
        if payment is None or payment.checkout_id is None:
            existing_payment = await uow.payments.get_payment(payment_id)
            if existing_payment is None or existing_payment.checkout_id is None:
                raise ResourceNotFoundError("payment not found")
            raise ForbiddenError("payment is not owned by requester")
        existing_key = None
        if key_hash is not None:
            existing_key = await uow.idempotency_keys.find_idempotency_key(
                PAYMENT_AUTH_RESULT_IDEMPOTENCY_SCOPE,
                key_hash,
            )
            if existing_key is not None and existing_key.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was used with another payload"
                )
            if existing_key is not None and existing_key.response_body is not None:
                return _payment_auth_failure_result_from_response_body(
                    existing_key.response_body
                )
            if existing_key is not None and existing_key.status == "processing":
                raise InvalidStateTransitionError(
                    "payment auth result is processing"
                )
        if payment.order_id != command.order_id:
            raise BadRequestError("order id does not match payment")
        checkout = await uow.checkouts.get_checkout_for_user(
            payment.checkout_id,
            user_id,
        )
        if checkout is None:
            raise ResourceNotFoundError("checkout not found")
        if payment.status == "failed" and payment.failure is not None:
            result = _auth_failure_result(payment)
            if key_hash is not None:
                await uow.idempotency_keys.save_idempotency_key(
                    _succeeded_idempotency_key(
                        existing_key=existing_key,
                        scope=PAYMENT_AUTH_RESULT_IDEMPOTENCY_SCOPE,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        now=now,
                        resource_type="payment",
                        resource_id=payment.id,
                        response_body=_auth_failure_result_to_response_body(result),
                    )
                )
            return result
        if payment.status != "ready":
            raise InvalidStateTransitionError("payment auth result cannot be recorded")

        if payment.expires_at is not None and payment.expires_at <= now:
            payment.status = "expired"
            payment.failure = {
                "phase": "before_confirm",
                "reason": "auth_result_not_reported",
                "retryable": True,
            }
            checkout.status = "expired"
            await _release_checkout_reserved_stock(checkout, uow.one_time_skus)
            await uow.payments.save_payment(payment)
            await uow.checkouts.save_checkout(checkout)
            result = _auth_failure_result(payment)
            if key_hash is not None:
                await uow.idempotency_keys.save_idempotency_key(
                    _succeeded_idempotency_key(
                        existing_key=existing_key,
                        scope=PAYMENT_AUTH_RESULT_IDEMPOTENCY_SCOPE,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        now=now,
                        resource_type="payment",
                        resource_id=payment.id,
                        response_body=_auth_failure_result_to_response_body(result),
                    )
                )
            return result

        payment.status = "failed"
        payment.failure = {
            "phase": "before_confirm",
            "reason": (
                "user_canceled"
                if command.code == "PAY_PROCESS_CANCELED"
                else "auth_failed"
            ),
            "providerCode": command.code,
            "message": command.message,
            "retryable": True,
        }
        checkout.status = "failed"
        await _release_checkout_reserved_stock(checkout, uow.one_time_skus)
        await uow.payments.save_payment(payment)
        await uow.checkouts.save_checkout(checkout)
        result = _auth_failure_result(payment)
        if key_hash is not None:
            await uow.idempotency_keys.save_idempotency_key(
                _succeeded_idempotency_key(
                    existing_key=existing_key,
                    scope=PAYMENT_AUTH_RESULT_IDEMPOTENCY_SCOPE,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    now=now,
                    resource_type="payment",
                    resource_id=payment.id,
                    response_body=_auth_failure_result_to_response_body(result),
                )
            )
        return result


async def _mark_payment_confirm_failed(
    *,
    requester_user_id: str,
    command: PaymentConfirmCommand,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    processing_key: IdempotencyKey,
    key_hash: str,
    request_hash: str,
    clock: Clock,
    message: str,
    provider_code: str | None,
    retryable: bool,
) -> NoReturn:
    now = clock.utc_now()
    async with one_time_payment_uow_factory() as uow:
        payment = await uow.payments.get_payment_for_user(
            command.payment_id,
            requester_user_id,
        )
        if payment is None or payment.checkout_id is None:
            raise ResourceNotFoundError("payment not found")
        checkout = await uow.checkouts.get_checkout_for_user(
            payment.checkout_id,
            requester_user_id,
        )
        if checkout is None:
            raise ResourceNotFoundError("checkout not found")
        if payment.status == "ready":
            payment.status = "failed"
            payment.failure = {
                "code": "PAYMENT_CONFIRM_FAILED",
                "providerCode": provider_code or "PROVIDER_CONFIRM_FAILED",
                "message": message,
                "retryable": retryable,
                "phase": "confirm",
                "reason": "provider_rejected" if provider_code else "provider_error",
            }
            checkout.status = "failed"
            await _release_checkout_reserved_stock(checkout, uow.one_time_skus)
            await uow.payments.save_payment(payment)
            await uow.checkouts.save_checkout(checkout)
        if payment.status != "failed" or payment.failure is None:
            raise InvalidStateTransitionError("payment confirmation failed")
        result = _auth_failure_result(payment)
        response_body = _auth_failure_result_to_response_body(result)
        await uow.idempotency_keys.save_idempotency_key(
            _failed_idempotency_key(
                existing_key=processing_key,
                scope=PAYMENT_CONFIRM_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                now=now,
                resource_type="payment",
                resource_id=payment.id,
                response_body=response_body,
            )
        )
    raise PaymentConfirmRejectedError(response_body)


async def _mark_payment_confirm_validation_failed(
    *,
    payment: Payment,
    checkout: Checkout,
    one_time_skus: OneTimeSkuRepository,
    payments: PaymentAttemptRepository,
    checkouts: CheckoutRepository,
    message: str,
) -> None:
    payment.status = "failed"
    payment.failure = {
        "code": "PAYMENT_CONFIRM_VALIDATION_FAILED",
        "providerCode": "PAYMENT_CONFIRM_VALIDATION_FAILED",
        "message": message,
        "retryable": True,
        "phase": "confirm",
        "reason": "validation_failed",
    }
    checkout.status = "failed"
    await _release_checkout_reserved_stock(checkout, one_time_skus)
    await payments.save_payment(payment)
    await checkouts.save_checkout(checkout)


async def _resolve_checkout(
    user_id: str,
    checkout_id: str | None,
    items: list[PricedPaymentOrderItem],
    payment_customer_id: str,
    checkouts: CheckoutRepository,
    now: datetime,
) -> Checkout:
    if checkout_id:
        checkout = await checkouts.get_checkout_for_user(checkout_id, user_id)
        if checkout is None:
            raise ResourceNotFoundError("checkout not found")
        if checkout.status not in {"failed", "expired"}:
            raise InvalidStateTransitionError("checkout cannot be retried")
        if not _checkout_items_match_priced_items(checkout, items):
            raise InvalidStateTransitionError("checkout items do not match")
        return checkout

    return Checkout(
        id=Checkout.generate_id(),
        user_id=user_id,
        payment_customer_id=payment_customer_id,
        items=[
            {
                "skuId": item.sku_id,
                "quantity": item.quantity,
                "unitAmount": item.unit_amount,
                "amount": item.amount,
            }
            for item in items
        ],
        status="ready",
        created_at=now,
        )


def _checkout_items_match_priced_items(
    checkout: Checkout,
    items: list[PricedPaymentOrderItem],
) -> bool:
    expected = sorted(
        (
            item.sku_id,
            item.quantity,
            item.unit_amount,
            item.amount,
        )
        for item in items
    )
    actual: list[tuple[str, int, int, int]] = []
    for item in checkout.items:
        sku_id = item.get("skuId")
        quantity = item.get("quantity")
        unit_amount = item.get("unitAmount")
        amount = item.get("amount")
        if (
            not isinstance(sku_id, str)
            or not isinstance(quantity, int)
            or not isinstance(unit_amount, int)
            or not isinstance(amount, int)
        ):
            return False
        actual.append((sku_id, quantity, unit_amount, amount))
    return sorted(actual) == expected


async def _get_or_create_payment_customer(
    payment_customers: PaymentCustomerRepository,
    user_id: str,
) -> PaymentCustomer:
    existing = await payment_customers.get_active_payment_customer_for_user(user_id)
    if existing is not None:
        return existing
    customer = PaymentCustomer(
        id=PaymentCustomer.generate_id(),
        user_id=user_id,
        provider="tosspayments",
        customer_key=PaymentCustomer.generate_pcus_key(),
        status="active",
    )
    await payment_customers.save_payment_customer(customer)
    return customer


async def _release_checkout_reserved_stock(
    checkout: Checkout,
    one_time_skus: OneTimeSkuRepository,
) -> None:
    for item in checkout.items:
        sku_id = item.get("skuId")
        quantity = item.get("quantity")
        if isinstance(sku_id, str) and isinstance(quantity, int):
            await one_time_skus.release_reserved_one_time_sku_stock(
                sku_id,
                quantity,
            )


async def _capture_checkout_reserved_stock(
    checkout: Checkout,
    one_time_skus: OneTimeSkuRepository,
) -> None:
    for item in checkout.items:
        sku_id = item.get("skuId")
        quantity = item.get("quantity")
        if isinstance(sku_id, str) and isinstance(quantity, int):
            await one_time_skus.capture_reserved_one_time_sku_stock(
                sku_id,
                quantity,
            )


async def _restore_checkout_sold_stock(
    checkout: Checkout,
    one_time_skus: OneTimeSkuRepository,
) -> None:
    for item in checkout.items:
        sku_id = item.get("skuId")
        quantity = item.get("quantity")
        if isinstance(sku_id, str) and isinstance(quantity, int):
            await one_time_skus.restore_sold_one_time_sku_stock(
                sku_id,
                quantity,
            )


async def _mark_payment_cancel_request_failed(
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    processing_key: IdempotencyKey,
    pending_cancel_request: PaymentCancelRequest,
    request_hash: str,
    now: datetime,
    reason: str,
    provider_code: str | None,
    retryable: bool,
) -> None:
    failure = _provider_failure_summary(
        message=reason,
        provider_code=provider_code,
        retryable=retryable,
    )
    async with one_time_payment_uow_factory() as uow:
        failed_cancel_request = PaymentCancelRequest(
            id=pending_cancel_request.id,
            payment_id=pending_cancel_request.payment_id,
            idempotency_key_hash=pending_cancel_request.idempotency_key_hash,
            status="failed",
            cancel_amount=pending_cancel_request.cancel_amount,
            cancel_reason=pending_cancel_request.cancel_reason,
            requested_by=pending_cancel_request.requested_by,
            requested_user_id=pending_cancel_request.requested_user_id,
            operator_audit_id=pending_cancel_request.operator_audit_id,
            created_at=pending_cancel_request.created_at,
            updated_at=now,
            failure=failure,
        )
        await uow.payment_cancel_requests.save_payment_cancel_request(
            failed_cancel_request
        )
        payment = await uow.payments.get_payment(pending_cancel_request.payment_id)
        if (
            payment is not None
            and pending_cancel_request.operator_audit_id is not None
            and pending_cancel_request.requested_user_id is not None
        ):
            await uow.operator_audits.save_operator_audit(
                OperatorAudit(
                    id=pending_cancel_request.operator_audit_id,
                    operator_id=pending_cancel_request.requested_user_id,
                    action="payment.cancel",
                    target_type="payment",
                    target_id=payment.id,
                    previous_state=_payment_cancel_audit_state(payment),
                    next_state={
                        **_payment_cancel_audit_state(payment),
                        "cancel_amount": pending_cancel_request.cancel_amount,
                        "cancel_reason": pending_cancel_request.cancel_reason,
                        "requested_by": pending_cancel_request.requested_by,
                        "failure": failed_cancel_request.failure or {},
                    },
                    reason_code=pending_cancel_request.cancel_reason,
                    result="failed",
                    created_at=now,
                    idempotency_key_id=processing_key.id,
                    idempotency_scope=PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                    idempotency_key_hash=processing_key.key_hash,
                    idempotency_request_hash=request_hash,
                )
            )
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=processing_key.id,
                scope=PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                key_hash=processing_key.key_hash,
                request_hash=request_hash,
                status="failed",
                created_at=processing_key.created_at,
                updated_at=now,
                expires_at=processing_key.expires_at,
                resource_type="payment_cancel_request",
                resource_id=pending_cancel_request.id,
            )
        )


def _provider_failure_summary(
    *,
    message: str,
    provider_code: str | None,
    retryable: bool,
) -> dict[str, object]:
    failure: dict[str, object] = {"message": message, "retryable": retryable}
    if provider_code is not None:
        failure["providerCode"] = provider_code
    return failure


def _payment_cancel_result_from_response_body(
    body: dict[str, object],
) -> PaymentCancelResult:
    return PaymentCancelResult(
        payment_id=str(body["paymentId"]),
        payment_key=str(body["paymentKey"]),
        status=str(body["status"]),
        paid_amount=_body_int(body, "paidAmount"),
        canceled_amount=_body_int(body, "canceledAmount"),
        cancelable_amount=_body_int(body, "cancelableAmount"),
        latest_cancel=_body_object_dict(body["latestCancel"]),
        cancel_history=_body_object_dict_list(body["cancelHistory"]),
    )


def _payment_auth_failure_result_from_response_body(
    body: dict[str, object],
) -> PaymentAuthFailureResult:
    return PaymentAuthFailureResult(
        checkout_id=str(body["checkoutId"]),
        payment_id=str(body["paymentId"]),
        order_id=str(body["orderId"]),
        status=str(body["status"]),
        failure=_body_object_dict(body["failure"]),
        retry=_body_object_dict(body["retry"]),
    )


def _payment_detail(
    payment: Payment,
    checkout: Checkout,
    attempt_no: int,
) -> PaymentDetail:
    return PaymentDetail(
        checkout_id=checkout.id,
        payment_id=payment.id,
        order_id=payment.order_id,
        attempt_no=attempt_no,
        status=payment.status,
        amount=payment.amount,
        currency=_currency_from_checkout(checkout),
        order_name=_order_name_from_checkout(checkout),
        approved_at=payment.approved_at,
        receipt_url=payment.receipt_url,
        method=payment.method,
        method_detail=payment.method_detail,
        failure=payment.failure,
        retry=_retry_for_payment(payment),
    )


def _payment_confirm_result(
    payment: Payment,
    attempt_no: int,
) -> PaymentConfirmResult:
    if payment.approved_at is None:
        raise InvalidStateTransitionError("payment has no approved time")
    return PaymentConfirmResult(
        checkout_id=payment.checkout_id or "",
        payment_id=payment.id,
        order_id=payment.order_id,
        attempt_no=attempt_no,
        payment_key=payment.payment_key or "",
        status=payment.status,
        amount=payment.amount,
        currency="KRW",
        approved_at=payment.approved_at,
        receipt_url=payment.receipt_url,
        method=payment.method or "",
    )


def _payment_confirm_result_from_response_body(
    body: dict[str, object],
) -> PaymentConfirmResult:
    approved_at = body["approvedAt"]
    if not isinstance(approved_at, (datetime, str)):
        raise InvalidStateTransitionError("idempotency response approvedAt is invalid")
    return PaymentConfirmResult(
        checkout_id=str(body["checkoutId"]),
        payment_id=str(body["paymentId"]),
        order_id=str(body["orderId"]),
        attempt_no=_body_int(body, "attemptNo"),
        payment_key=str(body["paymentKey"]),
        status=str(body["status"]),
        amount=_body_int(body, "amount"),
        currency=str(body["currency"]),
        approved_at=(
            datetime.fromisoformat(approved_at)
            if isinstance(approved_at, str)
            else approved_at
        ),
        receipt_url=(
            str(body["receiptUrl"]) if body.get("receiptUrl") is not None else None
        ),
        method=str(body["method"]),
    )


def _confirm_result_to_response_body(
    result: PaymentConfirmResult,
) -> dict[str, object]:
    return {
        "checkoutId": result.checkout_id,
        "paymentId": result.payment_id,
        "orderId": result.order_id,
        "attemptNo": result.attempt_no,
        "paymentKey": result.payment_key,
        "status": result.status,
        "amount": result.amount,
        "currency": result.currency,
        "approvedAt": result.approved_at,
        "receiptUrl": result.receipt_url,
        "method": result.method,
    }


def _cancel_result_to_response_body(result: PaymentCancelResult) -> dict[str, object]:
    return {
        "paymentId": result.payment_id,
        "paymentKey": result.payment_key,
        "status": result.status,
        "paidAmount": result.paid_amount,
        "canceledAmount": result.canceled_amount,
        "cancelableAmount": result.cancelable_amount,
        "latestCancel": result.latest_cancel,
        "cancelHistory": result.cancel_history,
    }


def _provider_cancel_response_mismatches(
    *,
    provider_result: PaymentCancelProviderResult,
    cancel_amount: int,
    paid_amount: int,
    expected_cancelable_amount: int,
) -> bool:
    return (
        provider_result.cancel_id == ""
        or provider_result.cancel_amount != cancel_amount
        or provider_result.cancelable_amount != expected_cancelable_amount
        or provider_result.canceled_amount
        != paid_amount - expected_cancelable_amount
    )


def _auth_failure_result_to_response_body(
    result: PaymentAuthFailureResult,
) -> dict[str, object]:
    return {
        "checkoutId": result.checkout_id,
        "paymentId": result.payment_id,
        "orderId": result.order_id,
        "status": result.status,
        "failure": result.failure,
        "retry": result.retry,
    }


def _succeeded_idempotency_key(
    *,
    existing_key: IdempotencyKey | None,
    scope: str,
    key_hash: str,
    request_hash: str,
    now: datetime,
    resource_type: str,
    resource_id: str,
    response_body: dict[str, object],
) -> IdempotencyKey:
    return IdempotencyKey(
        id=(
            existing_key.id
            if existing_key is not None
            else IdempotencyKey.generate_id()
        ),
        scope=scope,
        key_hash=key_hash,
        request_hash=request_hash,
        status="succeeded",
        created_at=existing_key.created_at if existing_key is not None else now,
        updated_at=now,
        expires_at=existing_key.expires_at
        if existing_key is not None
        else now + timedelta(hours=24),
        resource_type=resource_type,
        resource_id=resource_id,
        response_status=200,
        response_body=response_body,
    )


def _failed_idempotency_key(
    *,
    existing_key: IdempotencyKey | None,
    scope: str,
    key_hash: str,
    request_hash: str,
    now: datetime,
    resource_type: str,
    resource_id: str,
    response_body: dict[str, object],
) -> IdempotencyKey:
    return IdempotencyKey(
        id=(
            existing_key.id
            if existing_key is not None
            else IdempotencyKey.generate_id()
        ),
        scope=scope,
        key_hash=key_hash,
        request_hash=request_hash,
        status="failed",
        created_at=existing_key.created_at if existing_key is not None else now,
        updated_at=now,
        expires_at=existing_key.expires_at
        if existing_key is not None
        else now + timedelta(hours=24),
        resource_type=resource_type,
        resource_id=resource_id,
        response_status=402,
        response_body=response_body,
    )


def _body_int(body: dict[str, object], key: str) -> int:
    value = body[key]
    if not isinstance(value, int):
        raise InvalidStateTransitionError("idempotency response number is invalid")
    return value


def _body_object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidStateTransitionError("idempotency response object is invalid")
    return {str(key): item for key, item in value.items()}


def _body_object_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise InvalidStateTransitionError("idempotency response list is invalid")
    items: list[dict[str, object]] = []
    for item in value:
        items.append(_body_object_dict(item))
    return items


def _cancelable_amount(payment: Payment) -> int:
    if payment.cancelable_amount is not None:
        return payment.cancelable_amount
    canceled_amount = sum(
        int(cancel.get("cancelAmount", 0)) for cancel in (payment.cancel_history or [])
    )
    return max(payment.amount - canceled_amount, 0)


def _provider_cancel_ids(
    cancel_history: list[dict[str, object]] | None,
) -> set[str]:
    ids: set[str] = set()
    for cancel in cancel_history or []:
        provider_cancel_id = cancel.get("providerCancelId") or cancel.get("cancelId")
        if isinstance(provider_cancel_id, str) and provider_cancel_id:
            ids.add(provider_cancel_id)
    return ids


def _payment_cancel_notification(*, cancel_amount: int) -> dict[str, object]:
    return {
        "template": "payment_cancel_completed",
        "queued": True,
        "payload": {
            "cancelAmount": cancel_amount,
        },
    }


def _payment_cancel_audit_state(payment: Payment) -> dict[str, object]:
    return {
        "payment_id": payment.id,
        "status": payment.status,
        "amount": payment.amount,
        "payment_key": payment.payment_key,
        "cancelable_amount": _cancelable_amount(payment),
        "cancel_history": list(payment.cancel_history or []),
    }


def _auth_failure_result(payment: Payment) -> PaymentAuthFailureResult:
    checkout_id = payment.checkout_id or ""
    return PaymentAuthFailureResult(
        checkout_id=checkout_id,
        payment_id=payment.id,
        order_id=payment.order_id,
        status=payment.status,
        failure=payment.failure or {},
        retry={
            "available": True,
            "action": "create_new_payment_attempt",
            "checkoutId": checkout_id,
        },
    )


def _retry_for_payment(payment: Payment) -> dict[str, object]:
    checkout_id = payment.checkout_id or ""
    if payment.status in {"failed", "expired"} and (
        payment.failure is None or payment.failure.get("retryable") is not False
    ):
        return {
            "available": True,
            "action": "create_new_payment_attempt",
            "checkoutId": checkout_id,
        }
    return {"available": False}


def _require_user_id(requester: RequestContext) -> str:
    if not requester.user_id:
        raise AuthorizationError("request user id is required")
    return requester.user_id


def _validate_items(items: list[PaymentOrderItem]) -> None:
    if not items:
        raise InvalidStateTransitionError("items are required")
    if any(item.quantity < 1 for item in items):
        raise InvalidStateTransitionError("item quantity must be positive")


async def _price_order_items(
    one_time_skus: OneTimeSkuRepository,
    items: list[PaymentOrderItem],
) -> PaymentOrderPricing:
    priced_items: list[PricedPaymentOrderItem] = []
    total_amount = 0
    for item in items:
        sku = await one_time_skus.get_active_one_time_sku(item.sku_id)
        if sku is None:
            raise ResourceNotFoundError("one-time sku not found")
        _validate_sku_purchase(sku, item.quantity)
        item_amount = sku.amount * item.quantity
        priced_items.append(
            PricedPaymentOrderItem(
                sku_id=sku.id,
                quantity=item.quantity,
                unit_amount=sku.amount,
                amount=item_amount,
                purchase_limit=_purchase_limit_policy(sku.purchase_limit),
            )
        )
        total_amount += item_amount
    return PaymentOrderPricing(items=priced_items, amount=total_amount)


def _validate_sku_purchase(sku: OneTimeSku, quantity: int) -> None:
    if sku.amount < 1:
        raise InvalidStateTransitionError("sku amount must be positive")
    purchase_limit = _purchase_limit_policy(sku.purchase_limit)
    per_order = _purchase_limit_value(purchase_limit, "perOrder")
    if per_order is not None and quantity > per_order:
        raise InvalidStateTransitionError("sku purchase limit is exceeded")
    if sku.stock_policy == "unlimited":
        return
    available_stock = sku.available_stock
    if available_stock is None or available_stock < quantity:
        raise InvalidStateTransitionError("sku stock is not available")


async def _validate_order_purchase_limits(
    *,
    user_id: str,
    items: list[PricedPaymentOrderItem],
    payments: PaymentAttemptRepository,
) -> None:
    requested_quantities: dict[str, int] = {}
    per_user_limits: dict[str, int] = {}
    for item in items:
        requested_quantities[item.sku_id] = (
            requested_quantities.get(item.sku_id, 0) + item.quantity
        )
        per_user = _purchase_limit_value(item.purchase_limit, "perUser")
        if per_user is not None:
            per_user_limits[item.sku_id] = per_user

    committed_statuses = {"ready", "paid", "partial_canceled", "canceled"}
    for sku_id, per_user in per_user_limits.items():
        committed_quantity = await payments.count_user_payment_quantity_for_sku(
            user_id,
            sku_id,
            committed_statuses,
        )
        if committed_quantity + requested_quantities[sku_id] > per_user:
            raise InvalidStateTransitionError("sku purchase limit is exceeded")


def _purchase_limit_policy(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidStateTransitionError("sku purchase limit is invalid")
    return {str(key): item for key, item in value.items()}


def _purchase_limit_value(
    purchase_limit: dict[str, object] | None,
    key: str,
) -> int | None:
    if purchase_limit is None:
        return None
    value = purchase_limit.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidStateTransitionError("sku purchase limit is invalid")
    return value


async def _reserve_priced_order_items(
    one_time_skus: OneTimeSkuRepository,
    items: list[PricedPaymentOrderItem],
) -> None:
    for item in items:
        sku = await one_time_skus.get_active_one_time_sku(item.sku_id)
        if sku is None:
            raise ResourceNotFoundError("one-time sku not found")
        reserved = await one_time_skus.reserve_one_time_sku_stock(
            sku,
            item.quantity,
        )
        if not reserved:
            raise InvalidStateTransitionError("sku stock is not available")


def _hash_payload(payload: dict) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result_to_response_body(result: PaymentOrderResult) -> dict:
    return {
        "checkoutId": result.checkout_id,
        "paymentId": result.payment_id,
        "orderId": result.order_id,
        "attemptNo": result.attempt_no,
        "orderName": result.order_name,
        "amount": result.amount,
        "currency": result.currency,
        "customerKey": result.customer_key,
        "clientKey": result.client_key,
        "successUrl": result.success_url,
        "failUrl": result.fail_url,
        "status": result.status,
        "expiresAt": result.expires_at.isoformat(),
    }


def _append_payment_id(url: str, payment_id: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["paymentId"] = payment_id
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _order_name(items: list[PricedPaymentOrderItem]) -> str:
    first = items[0]
    if len(items) == 1:
        return first.sku_id.removeprefix("sku_").upper()
    return f"{first.sku_id.removeprefix('sku_').upper()} 외 {len(items) - 1}건"


def _order_name_from_checkout(checkout: Checkout) -> str:
    if not checkout.items:
        return ""
    first_sku_id = str(checkout.items[0].get("skuId", ""))
    if len(checkout.items) == 1:
        return first_sku_id.removeprefix("sku_").upper()
    return f"{first_sku_id.removeprefix('sku_').upper()} 외 {len(checkout.items) - 1}건"


def _currency(items: list[PricedPaymentOrderItem]) -> str:
    if not items:
        return "KRW"
    return "KRW"


def _currency_from_checkout(checkout: Checkout) -> str:
    if not checkout.items:
        return "KRW"
    return "KRW"
