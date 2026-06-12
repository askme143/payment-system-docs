from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from payments.application.admin_catalog import AdminRequestContext
from payments.application.cursors import encode_cursor
from payments.application.errors import (
    BadRequestError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ProviderError,
    ResourceNotFoundError,
)
from payments.application.operation_locks import (
    acquire_required_operation_lock,
    release_operation_lock,
)
from payments.application.ports.admin_operations import (
    AdminListQuery,
    AdminOperationsRepository,
    AdminPaymentListRecord,
    AdminSubscriptionListRecord,
)
from payments.application.ports.clock import Clock
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.one_time_skus import OneTimeSkuRepository
from payments.application.ports.operation_locks import OperationLockRepository
from payments.application.ports.provider import (
    PaymentCancelProviderResult,
    PaymentLookupProviderResult,
    PaymentProvider,
)
from payments.application.ports.unit_of_work import (
    AdminSubscriptionAdjustUnitOfWorkFactory,
    OneTimePaymentUnitOfWorkFactory,
)
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.ids import generate_uuid_id
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_cancel_request import PaymentCancelRequest
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan

ADMIN_PAYMENT_CANCEL_IDEMPOTENCY_SCOPE = "admin-payment-cancel"
ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE = "admin-subscription-adjust"
_ADMIN_PAYMENT_STATUSES = frozenset(
    {"ready", "paid", "partial_canceled", "canceled", "failed", "expired"}
)
_ADMIN_SUBSCRIPTION_LIST_FILTER_STATUSES = frozenset(
    {"active", "cancel_scheduled", "past_due", "canceled"}
)

SubscriptionAdjustmentType = Literal[
    "provider_payment_sync",
    "postpone_next_billing",
    "set_next_billing_date",
    "clear_payment_failure",
    "status_override",
]
AdminSubscriptionStatus = Literal[
    "active",
    "past_due",
    "cancel_scheduled",
    "canceled",
]


@dataclass(frozen=True, slots=True)
class AdminPage:
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class AdminPaymentListItem:
    payment_id: str
    checkout_id: str | None
    user_id: str | None
    user_email: str | None
    order_id: str
    order_name: str
    payment_key: str | None
    status: str
    amount: int
    paid_amount: int
    cancelable_amount: int
    currency: str
    approved_at: datetime | None
    method_summary: str | None
    detail_url: str
    cancel_url: str | None


@dataclass(frozen=True, slots=True)
class AdminPaymentListResult:
    items: list[AdminPaymentListItem]
    page: AdminPage


@dataclass(frozen=True, slots=True)
class AdminSubscriptionListItem:
    subscription_id: str
    user_id: str
    user_email: str | None
    product_code: str
    product_name: str
    plan_id: str
    plan_name: str
    status: str
    current_period_start_at: datetime | None
    current_period_end_at: datetime | None
    next_billing_at: datetime | None
    payment_failure: dict[str, object] | None
    default_billing_method_summary: str | None
    detail_url: str
    adjust_url: str | None


@dataclass(frozen=True, slots=True)
class AdminSubscriptionListResult:
    items: list[AdminSubscriptionListItem]
    page: AdminPage


@dataclass(frozen=True, slots=True)
class AdminPaymentCancelCommand:
    cancel_amount: int | None
    cancel_reason: str
    reason_message: str
    notify_customer: bool = True


@dataclass(frozen=True, slots=True)
class AdminPaymentCancelResult:
    payment_id: str
    status: str
    paid_amount: int
    canceled_amount: int
    cancelable_amount: int
    operator_audit_id: str
    cancel_history: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class AdminSubscriptionAdjustCommand:
    adjustment_type: SubscriptionAdjustmentType
    reason_code: str
    reason_message: str
    payment_key: str | None = None
    invoice_id: str | None = None
    postpone_days: int | None = None
    next_billing_at: datetime | None = None
    target_status: AdminSubscriptionStatus | None = None
    notify_customer: bool = False


@dataclass(frozen=True, slots=True)
class AdminSubscriptionAdjustResult:
    subscription_id: str
    adjustment_type: SubscriptionAdjustmentType
    previous_state: dict[str, object]
    current_state: dict[str, object]
    operator_audit_id: str
    notified_customer: bool


@dataclass(frozen=True, slots=True)
class _ProviderPaymentSyncResult:
    payment: Payment
    invoice: Invoice | None
    previous_state: dict[str, object]
    current_state: dict[str, object]


@dataclass(frozen=True, slots=True)
class _PaymentFailureClearResult:
    payment: Payment
    invoice: Invoice | None
    previous_state: dict[str, object]
    current_state: dict[str, object]


async def list_admin_payments(
    query: AdminListQuery,
    repository: AdminOperationsRepository,
    context: AdminRequestContext | None = None,
    clock: Clock | None = None,
) -> AdminPaymentListResult:
    """관리자 결제 목록을 조회합니다.

    Args:
        query: 운영 콘솔 검색 조건과 페이지 크기입니다.
        repository: 관리자 결제 목록 조회 저장소입니다.

    Returns:
        결제 목록과 페이지 정보입니다.
    """
    _validate_admin_payment_list_query(query)
    records = await repository.list_admin_payments(
        replace(query, limit=query.limit + 1)
    )
    page_records = records[: query.limit]
    has_more = len(records) > query.limit
    result = AdminPaymentListResult(
        items=[_payment_item(record) for record in page_records],
        page=AdminPage(
            next_cursor=(
                _admin_payment_next_cursor(page_records[-1])
                if has_more and page_records
                else None
            ),
            has_more=has_more,
        ),
    )
    if context is not None and clock is not None:
        await _save_admin_list_audit_record(
            repository=repository,
            context=context,
            clock=clock,
            action="payment.list",
            target_type="payment",
            target_id="admin-payments",
            query=_admin_payment_query_snapshot(query),
            result_count=len(result.items),
            has_more=result.page.has_more,
        )
    return result


async def cancel_admin_payment(
    context: AdminRequestContext,
    payment_id: str,
    command: AdminPaymentCancelCommand,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    provider: PaymentProvider,
    clock: Clock,
    idempotency_key: str,
    operation_locks: OperationLockRepository | None = None,
) -> AdminPaymentCancelResult:
    """관리자가 회원 소유권 검증 없이 일반결제를 취소합니다.

    Args:
        context: 인증된 관리자 요청 컨텍스트입니다.
        payment_id: 취소할 결제 ID입니다.
        command: 운영 취소 금액과 사유입니다.
        one_time_payment_uow_factory: 결제 상태를 저장하는 UoW입니다.
        provider: 결제 취소 provider 포트입니다.
        clock: 감사 ID 생성 시각 fallback에 사용할 시간 포트입니다.
        idempotency_key: 운영자 도구 재시도와 중복 클릭을 묶는 필수 키입니다.
        operation_locks: paymentId 단위 취소 동시 실행을 막는 lock 저장소입니다.

    Returns:
        운영자 취소 결과와 감사 ID입니다.

    Raises:
        ResourceNotFoundError: 결제가 없을 때 발생합니다.
        InvalidStateTransitionError: 취소할 수 없는 상태 또는 금액입니다.
    """
    _validate_admin_payment_cancel_command(command)
    now = clock.utc_now()
    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"payment-cancel:{payment_id}",
        fencing_counter_key="payment-cancel",
        now=now,
        metadata={
            "api": ADMIN_PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
            "paymentId": payment_id,
            "requestId": context.request_id,
            "adminId": context.admin_id,
            "idempotencyKeyHash": _hash_text(idempotency_key),
        },
    )
    try:
        return await _cancel_admin_payment_locked(
            context=context,
            payment_id=payment_id,
            command=command,
            one_time_payment_uow_factory=one_time_payment_uow_factory,
            provider=provider,
            clock=clock,
            idempotency_key=idempotency_key,
        )
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def _cancel_admin_payment_locked(
    context: AdminRequestContext,
    payment_id: str,
    command: AdminPaymentCancelCommand,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    provider: PaymentProvider,
    clock: Clock,
    idempotency_key: str,
) -> AdminPaymentCancelResult:
    payload = {
        "paymentId": payment_id,
        "adminId": context.admin_id,
        "cancelAmount": command.cancel_amount,
        "cancelReason": command.cancel_reason,
        "reasonMessage": command.reason_message,
        "notifyCustomer": command.notify_customer,
    }
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
    existing_provider_cancel_ids: set[str] = set()
    previous_state: dict[str, object] | None = None
    audit_id: str | None = None

    async with one_time_payment_uow_factory() as uow:
        payment = await uow.payments.get_payment(payment_id)
        if payment is None:
            raise ResourceNotFoundError("payment not found")
        existing_key = await uow.idempotency_keys.find_idempotency_key(
            ADMIN_PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _admin_payment_cancel_result_from_response_body(
                existing_key.response_body
            )
        if existing_key is not None and existing_key.status == "processing":
            raise InvalidStateTransitionError(
                "admin payment cancellation is processing"
            )
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
            raise InvalidStateTransitionError(
                "admin payment cancellation is processing"
            )
        audit_id = (
            existing_cancel_request.operator_audit_id
            if existing_cancel_request is not None
            and existing_cancel_request.operator_audit_id is not None
            else generate_uuid_id("audit")
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
            requested_by="admin",
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
            scope=ADMIN_PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            status="processing",
            created_at=existing_key.created_at if existing_key is not None else now,
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
        previous_state = _payment_audit_state(payment)
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
        or previous_state is None
        or audit_id is None
    ):
        raise InvalidStateTransitionError("admin payment cancellation was not prepared")

    try:
        provider_result = await provider.cancel_payment(
            payment_key=payment_key,
            cancel_amount=cancel_amount,
            cancel_reason=command.cancel_reason,
            refund_bank_account=None,
            idempotency_key=idempotency_key,
        )
    except ProviderError as exc:
        await _mark_admin_payment_cancel_request_failed(
            context=context,
            command=command,
            one_time_payment_uow_factory=one_time_payment_uow_factory,
            processing_key=processing_key,
            pending_cancel_request=pending_cancel_request,
            request_hash=request_hash,
            previous_state=previous_state,
            now=clock.utc_now(),
            message="provider cancel failed",
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
        await _mark_admin_payment_cancel_request_failed(
            context=context,
            command=command,
            one_time_payment_uow_factory=one_time_payment_uow_factory,
            processing_key=processing_key,
            pending_cancel_request=pending_cancel_request,
            request_hash=request_hash,
            previous_state=previous_state,
            now=clock.utc_now(),
            message="provider response does not match",
            provider_code=None,
            retryable=True,
        )
        raise ProviderError("provider response does not match")
    if provider_result.cancel_id in existing_provider_cancel_ids:
        await _mark_admin_payment_cancel_request_failed(
            context=context,
            command=command,
            one_time_payment_uow_factory=one_time_payment_uow_factory,
            processing_key=processing_key,
            pending_cancel_request=pending_cancel_request,
            request_hash=request_hash,
            previous_state=previous_state,
            now=clock.utc_now(),
            message="provider cancel id is duplicated",
            provider_code=None,
            retryable=True,
        )
        raise ProviderError("provider cancel id is duplicated")

    async with one_time_payment_uow_factory() as uow:
        payment = await uow.payments.get_payment(payment_id)
        if payment is None:
            raise ResourceNotFoundError("payment not found")
        if payment.status not in {"paid", "partial_canceled"}:
            raise InvalidStateTransitionError("payment cannot be canceled")
        if payment.payment_key != payment_key:
            raise InvalidStateTransitionError("payment key does not match")
        cancelable_amount = _cancelable_amount(payment)
        if cancel_amount < 1 or cancel_amount > cancelable_amount:
            raise BadRequestError("cancel amount is invalid")
        previous_state = _payment_audit_state(payment)
        latest_cancel = {
            "cancelId": pending_cancel_request.id,
            "providerCancelId": provider_result.cancel_id,
            "cancelAmount": provider_result.cancel_amount,
            "cancelReason": command.cancel_reason,
            "reasonMessage": command.reason_message,
            "canceledAt": provider_result.canceled_at,
            "receiptUrl": provider_result.receipt_url,
            "requestedBy": "admin",
            "adminId": context.admin_id,
            "operatorAuditId": audit_id,
            "notifyCustomer": command.notify_customer,
            "status": "succeeded",
        }
        cancel_history = [*(payment.cancel_history or []), latest_cancel]
        payment.cancel_history = cancel_history
        payment.cancelable_amount = max(cancelable_amount - cancel_amount, 0)
        payment.status = (
            "canceled" if payment.cancelable_amount == 0 else "partial_canceled"
        )
        if payment.status == "canceled" and payment.checkout_id is not None:
            checkout = await uow.checkouts.get_checkout(payment.checkout_id)
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
                operator_id=context.admin_id,
                action="payment.cancel",
                target_type="payment",
                target_id=payment.id,
                previous_state=previous_state,
                next_state={
                    **_payment_audit_state(payment),
                    "cancel_amount": cancel_amount,
                    "cancel_reason": command.cancel_reason,
                    "reason_message": command.reason_message,
                    "requested_by": "admin",
                    "notify_customer": command.notify_customer,
                    "notification": _payment_cancel_notification(
                        command.notify_customer,
                        cancel_amount=cancel_amount,
                    ),
                },
                reason_code=command.cancel_reason,
                result="succeeded",
                created_at=clock.utc_now(),
                idempotency_key_id=processing_key.id,
                idempotency_scope=ADMIN_PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                idempotency_key_hash=key_hash,
                idempotency_request_hash=request_hash,
                reason_message=command.reason_message,
                request_ip=context.request_ip,
            )
        )
        result = AdminPaymentCancelResult(
            payment_id=payment.id,
            status=payment.status,
            paid_amount=payment.amount,
            canceled_amount=sum(
                int(cancel.get("cancelAmount", 0)) for cancel in cancel_history
            ),
            cancelable_amount=payment.cancelable_amount,
            operator_audit_id=audit_id,
            cancel_history=cancel_history,
        )
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=processing_key.id,
                scope=ADMIN_PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="succeeded",
                created_at=processing_key.created_at,
                updated_at=clock.utc_now(),
                expires_at=processing_key.expires_at,
                resource_type="payment_cancel_request",
                resource_id=succeeded_cancel_request.id,
                response_status=200,
                response_body=_admin_payment_cancel_result_to_response_body(result),
            )
        )
        return result


async def list_admin_subscriptions(
    query: AdminListQuery,
    repository: AdminOperationsRepository,
    context: AdminRequestContext | None = None,
    clock: Clock | None = None,
) -> AdminSubscriptionListResult:
    """관리자 구독 목록을 조회합니다.

    Args:
        query: 운영 콘솔 검색 조건과 페이지 크기입니다.
        repository: 관리자 구독 목록 조회 저장소입니다.

    Returns:
        구독 목록과 페이지 정보입니다.
    """
    _validate_admin_subscription_list_query(query)
    records = await repository.list_admin_subscriptions(
        replace(query, limit=query.limit + 1)
    )
    page_records = records[: query.limit]
    has_more = len(records) > query.limit
    result = AdminSubscriptionListResult(
        items=[_subscription_item(record) for record in page_records],
        page=AdminPage(
            next_cursor=(
                _admin_subscription_next_cursor(page_records[-1])
                if has_more and page_records
                else None
            ),
            has_more=has_more,
        ),
    )
    if context is not None and clock is not None:
        await _save_admin_list_audit_record(
            repository=repository,
            context=context,
            clock=clock,
            action="subscription.list",
            target_type="subscription",
            target_id="admin-subscriptions",
            query=_admin_subscription_query_snapshot(query),
            result_count=len(result.items),
            has_more=result.page.has_more,
        )
    return result


async def adjust_admin_subscription(
    context: AdminRequestContext,
    subscription_id: str,
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
    idempotency_keys: IdempotencyKeyRepository,
    clock: Clock,
    idempotency_key: str,
    operation_locks: OperationLockRepository | None = None,
    provider: PaymentProvider | None = None,
    admin_subscription_adjust_uow_factory: (
        AdminSubscriptionAdjustUnitOfWorkFactory | None
    ) = None,
) -> AdminSubscriptionAdjustResult:
    """관리자가 구독의 결제일과 상태를 감사 가능하게 보정합니다."""
    _validate_admin_subscription_adjust_command(command)
    payload = {
        "subscriptionId": subscription_id,
        "adminId": context.admin_id,
        "adjustmentType": command.adjustment_type,
        "paymentKey": command.payment_key,
        "invoiceId": command.invoice_id,
        "postponeDays": command.postpone_days,
        "nextBillingAt": command.next_billing_at.isoformat()
        if command.next_billing_at is not None
        else None,
        "targetStatus": command.target_status,
        "reasonCode": command.reason_code,
        "reasonMessage": command.reason_message,
        "notifyCustomer": command.notify_customer,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key)
    now = clock.utc_now()
    existing_key = await idempotency_keys.find_idempotency_key(
        ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE,
        key_hash,
    )
    if existing_key is not None and existing_key.request_hash != request_hash:
        raise IdempotencyConflictError(
            "idempotency key was used with another payload"
        )
    if existing_key is not None and existing_key.response_body is not None:
        return _admin_subscription_adjust_result_from_response_body(
            existing_key.response_body
        )
    if existing_key is not None and existing_key.status == "processing":
        raise InvalidStateTransitionError("admin subscription adjustment is processing")

    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"subscription:{subscription_id}",
        fencing_counter_key="subscription",
        now=now,
        metadata={
            "api": ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE,
            "request_id": context.request_id,
            "subscription_id": subscription_id,
            "admin_id": context.admin_id,
        },
    )
    try:
        subscription = await repository.get_admin_subscription(subscription_id)
        if subscription is None:
            raise ResourceNotFoundError("subscription not found")

        sync_result: _ProviderPaymentSyncResult | None = None
        clear_result: _PaymentFailureClearResult | None = None
        previous_state = _subscription_state(subscription)
        audit_id = generate_uuid_id("audit")
        processing_key = IdempotencyKey(
            id=(
                existing_key.id
                if existing_key is not None
                else IdempotencyKey.generate_id()
            ),
            scope=ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            status="processing",
            created_at=existing_key.created_at if existing_key is not None else now,
            updated_at=now,
            expires_at=now + timedelta(hours=24),
            resource_type="subscription_adjustment",
            resource_id=audit_id,
            locked_until_at=now + timedelta(minutes=5),
        )
        processing_key_saved = False
        try:
            if command.adjustment_type == "provider_payment_sync":
                if provider is None:
                    raise ProviderError("provider payment sync is not configured")
                await idempotency_keys.save_idempotency_key(processing_key)
                processing_key_saved = True
                sync_result = await _apply_provider_payment_sync(
                    subscription=subscription,
                    command=command,
                    repository=repository,
                    provider=provider,
                )
                previous_state = sync_result.previous_state
                current_state = sync_result.current_state
            elif command.adjustment_type == "clear_payment_failure":
                clear_result = await _apply_clear_payment_failure(
                    subscription=subscription,
                    command=command,
                    repository=repository,
                )
                previous_state = clear_result.previous_state
                current_state = clear_result.current_state
            else:
                _apply_subscription_adjustment(subscription, command, now)
                current_state = _subscription_state(subscription)
        except ProviderError as exc:
            await _save_failed_subscription_adjustment(
                audit_id=audit_id,
                context=context,
                command=command,
                repository=repository,
                idempotency_keys=idempotency_keys,
                admin_subscription_adjust_uow_factory=(
                    admin_subscription_adjust_uow_factory
                ),
                subscription=subscription,
                previous_state=previous_state,
                processing_key=processing_key if processing_key_saved else None,
                failed_at=clock.utc_now(),
                message=str(exc),
            )
            raise
        except Exception:
            if processing_key_saved:
                await _save_failed_subscription_adjustment_idempotency_key(
                    idempotency_keys,
                    processing_key,
                    clock.utc_now(),
                )
            raise
        if not processing_key_saved:
            await idempotency_keys.save_idempotency_key(processing_key)

        result = AdminSubscriptionAdjustResult(
            subscription_id=subscription.id,
            adjustment_type=command.adjustment_type,
            previous_state=previous_state,
            current_state=current_state,
            operator_audit_id=audit_id,
            notified_customer=command.notify_customer,
        )
        adjusted_payment: Payment | None = None
        adjusted_invoice: Invoice | None = None
        if sync_result is not None:
            adjusted_payment = sync_result.payment
            adjusted_invoice = sync_result.invoice
        elif clear_result is not None:
            adjusted_payment = clear_result.payment
            adjusted_invoice = clear_result.invoice
        await _save_subscription_adjustment_success(
            context=context,
            command=command,
            repository=repository,
            idempotency_keys=idempotency_keys,
            admin_subscription_adjust_uow_factory=(
                admin_subscription_adjust_uow_factory
            ),
            subscription=subscription,
            payment=adjusted_payment,
            invoice=adjusted_invoice,
            audit_id=audit_id,
            previous_state=previous_state,
            current_state=current_state,
            processing_key=processing_key,
            request_hash=request_hash,
            result=result,
            now=clock.utc_now(),
        )
        return result
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def _mark_admin_payment_cancel_request_failed(
    *,
    context: AdminRequestContext,
    command: AdminPaymentCancelCommand,
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory,
    processing_key: IdempotencyKey,
    pending_cancel_request: PaymentCancelRequest,
    request_hash: str,
    previous_state: dict[str, object],
    now: datetime,
    message: str,
    provider_code: str | None,
    retryable: bool,
) -> None:
    failure = _provider_failure_summary(
        message=message,
        provider_code=provider_code,
        retryable=retryable,
    )
    async with one_time_payment_uow_factory() as uow:
        await uow.payment_cancel_requests.save_payment_cancel_request(
            PaymentCancelRequest(
                id=pending_cancel_request.id,
                payment_id=pending_cancel_request.payment_id,
                idempotency_key_hash=pending_cancel_request.idempotency_key_hash,
                status="failed",
                cancel_amount=pending_cancel_request.cancel_amount,
                cancel_reason=pending_cancel_request.cancel_reason,
                requested_by=pending_cancel_request.requested_by,
                operator_audit_id=pending_cancel_request.operator_audit_id,
                created_at=pending_cancel_request.created_at,
                updated_at=now,
                failure=failure,
            )
        )
        if pending_cancel_request.operator_audit_id is not None:
            await uow.operator_audits.save_operator_audit(
                OperatorAudit(
                    id=pending_cancel_request.operator_audit_id,
                    operator_id=context.admin_id,
                    action="payment.cancel",
                    target_type="payment",
                    target_id=pending_cancel_request.payment_id,
                    previous_state=previous_state,
                    next_state={
                        **previous_state,
                        "cancel_amount": pending_cancel_request.cancel_amount,
                        "cancel_reason": pending_cancel_request.cancel_reason,
                        "reason_message": command.reason_message,
                        "requested_by": "admin",
                        "notify_customer": command.notify_customer,
                        "failure": failure,
                    },
                    reason_code=pending_cancel_request.cancel_reason,
                    result="failed",
                    created_at=now,
                    idempotency_key_id=processing_key.id,
                    idempotency_scope=ADMIN_PAYMENT_CANCEL_IDEMPOTENCY_SCOPE,
                    idempotency_key_hash=processing_key.key_hash,
                    idempotency_request_hash=request_hash,
                    reason_message=command.reason_message,
                    request_ip=context.request_ip,
                )
            )
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=processing_key.id,
                scope=processing_key.scope,
                key_hash=processing_key.key_hash,
                request_hash=request_hash,
                status="failed",
                created_at=processing_key.created_at,
                updated_at=now,
                expires_at=processing_key.expires_at,
                resource_type=processing_key.resource_type,
                resource_id=processing_key.resource_id,
            )
        )


async def _save_failed_subscription_adjustment_idempotency_key(
    idempotency_keys: IdempotencyKeyRepository,
    processing_key: IdempotencyKey,
    failed_at: datetime,
) -> None:
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=processing_key.id,
            scope=processing_key.scope,
            key_hash=processing_key.key_hash,
            request_hash=processing_key.request_hash,
            status="failed",
            created_at=processing_key.created_at,
            updated_at=failed_at,
            expires_at=processing_key.expires_at,
            resource_type=processing_key.resource_type,
            resource_id=processing_key.resource_id,
        )
    )


async def _save_failed_subscription_adjustment(
    *,
    audit_id: str,
    context: AdminRequestContext,
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
    idempotency_keys: IdempotencyKeyRepository,
    admin_subscription_adjust_uow_factory: (
        AdminSubscriptionAdjustUnitOfWorkFactory | None
    ),
    subscription: Subscription,
    previous_state: dict[str, object],
    processing_key: IdempotencyKey | None,
    failed_at: datetime,
    message: str,
) -> None:
    if admin_subscription_adjust_uow_factory is not None:
        async with admin_subscription_adjust_uow_factory() as uow:
            await _save_failed_subscription_adjustment_audit_record(
                audit_id=audit_id,
                context=context,
                command=command,
                repository=uow.admin_operations,
                subscription=subscription,
                previous_state=previous_state,
                message=message,
            )
            if processing_key is not None:
                await _save_failed_subscription_adjustment_idempotency_key(
                    uow.idempotency_keys,
                    processing_key,
                    failed_at,
                )
        return

    await _save_failed_subscription_adjustment_audit_record(
        audit_id=audit_id,
        context=context,
        command=command,
        repository=repository,
        subscription=subscription,
        previous_state=previous_state,
        message=message,
    )
    if processing_key is not None:
        await _save_failed_subscription_adjustment_idempotency_key(
            idempotency_keys,
            processing_key,
            failed_at,
        )


async def _save_subscription_adjustment_success(
    *,
    context: AdminRequestContext,
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
    idempotency_keys: IdempotencyKeyRepository,
    admin_subscription_adjust_uow_factory: (
        AdminSubscriptionAdjustUnitOfWorkFactory | None
    ),
    subscription: Subscription,
    payment: Payment | None,
    invoice: Invoice | None,
    audit_id: str,
    previous_state: dict[str, object],
    current_state: dict[str, object],
    processing_key: IdempotencyKey,
    request_hash: str,
    result: AdminSubscriptionAdjustResult,
    now: datetime,
) -> None:
    if admin_subscription_adjust_uow_factory is not None:
        async with admin_subscription_adjust_uow_factory() as uow:
            await _save_subscription_adjustment_success_with_repositories(
                context=context,
                command=command,
                repository=uow.admin_operations,
                idempotency_keys=uow.idempotency_keys,
                subscription=subscription,
                payment=payment,
                invoice=invoice,
                audit_id=audit_id,
                previous_state=previous_state,
                current_state=current_state,
                processing_key=processing_key,
                request_hash=request_hash,
                result=result,
                now=now,
            )
        return

    await _save_subscription_adjustment_success_with_repositories(
        context=context,
        command=command,
        repository=repository,
        idempotency_keys=idempotency_keys,
        subscription=subscription,
        payment=payment,
        invoice=invoice,
        audit_id=audit_id,
        previous_state=previous_state,
        current_state=current_state,
        processing_key=processing_key,
        request_hash=request_hash,
        result=result,
        now=now,
    )


async def _save_subscription_adjustment_success_with_repositories(
    *,
    context: AdminRequestContext,
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
    idempotency_keys: IdempotencyKeyRepository,
    subscription: Subscription,
    payment: Payment | None,
    invoice: Invoice | None,
    audit_id: str,
    previous_state: dict[str, object],
    current_state: dict[str, object],
    processing_key: IdempotencyKey,
    request_hash: str,
    result: AdminSubscriptionAdjustResult,
    now: datetime,
) -> None:
    await repository.save_admin_subscription(subscription)
    if payment is not None:
        await repository.save_admin_payment(payment)
    if invoice is not None:
        await repository.save_admin_invoice(invoice)
    await repository.save_subscription_adjustment_audit_record(
        audit_id=audit_id,
        subscription_id=subscription.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        adjustment_type=command.adjustment_type,
        reason_code=command.reason_code,
        reason_message=command.reason_message,
        previous=previous_state,
        next_value=_subscription_adjust_audit_state(current_state, command),
        notified_customer=command.notify_customer,
        request_ip=context.request_ip,
        idempotency_key_id=processing_key.id,
        idempotency_scope=processing_key.scope,
        idempotency_key_hash=processing_key.key_hash,
        idempotency_request_hash=processing_key.request_hash,
    )
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=processing_key.id,
            scope=processing_key.scope,
            key_hash=processing_key.key_hash,
            request_hash=request_hash,
            status="succeeded",
            created_at=processing_key.created_at,
            updated_at=now,
            expires_at=processing_key.expires_at,
            resource_type=processing_key.resource_type,
            resource_id=processing_key.resource_id,
            response_status=200,
            response_body=_admin_subscription_adjust_result_to_response_body(result),
        )
    )


def _admin_payment_cancel_result_to_response_body(
    result: AdminPaymentCancelResult,
) -> dict[str, object]:
    return {
        "paymentId": result.payment_id,
        "status": result.status,
        "paidAmount": result.paid_amount,
        "canceledAmount": result.canceled_amount,
        "cancelableAmount": result.cancelable_amount,
        "operatorAuditId": result.operator_audit_id,
        "cancelHistory": result.cancel_history,
    }


def _admin_payment_cancel_result_from_response_body(
    response_body: Mapping[str, object],
) -> AdminPaymentCancelResult:
    cancel_history = response_body.get("cancelHistory")
    if not isinstance(cancel_history, list):
        cancel_history = []
    return AdminPaymentCancelResult(
        payment_id=str(response_body["paymentId"]),
        status=str(response_body["status"]),
        paid_amount=_response_int(response_body["paidAmount"]),
        canceled_amount=_response_int(response_body["canceledAmount"]),
        cancelable_amount=_response_int(response_body["cancelableAmount"]),
        operator_audit_id=str(response_body["operatorAuditId"]),
        cancel_history=[
            dict(cancel)
            for cancel in cancel_history
            if isinstance(cancel, Mapping)
        ],
    )


def _response_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise InvalidStateTransitionError("idempotency response is invalid")


def _admin_payment_next_cursor(record: AdminPaymentListRecord) -> str | None:
    return encode_cursor(
        {
            "sortAt": record.approved_at or record.created_at,
            "paymentId": record.payment_id,
        }
    )


def _validate_admin_payment_list_query(query: AdminListQuery) -> None:
    status_values = _admin_list_status_values(query.status)
    invalid_statuses = [
        status for status in status_values if status not in _ADMIN_PAYMENT_STATUSES
    ]
    if invalid_statuses:
        raise BadRequestError("status is invalid")
    if (
        query.from_at is not None
        and query.to_at is not None
        and query.from_at > query.to_at
    ):
        raise BadRequestError("payment date range is invalid")


async def _save_admin_list_audit_record(
    *,
    repository: AdminOperationsRepository,
    context: AdminRequestContext,
    clock: Clock,
    action: str,
    target_type: str,
    target_id: str,
    query: dict[str, object],
    result_count: int,
    has_more: bool,
) -> None:
    await repository.save_admin_list_audit_record(
        audit_id=OperatorAudit.generate_id(),
        admin_id=context.admin_id,
        request_id=context.request_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        query=query,
        result_count=result_count,
        has_more=has_more,
        request_ip=context.request_ip,
        created_at=clock.utc_now(),
    )


def _admin_payment_query_snapshot(query: AdminListQuery) -> dict[str, object]:
    snapshot: dict[str, object] = {"limit": query.limit}
    status_values = _admin_list_status_values(query.status)
    if status_values:
        snapshot["status"] = list(status_values)
    if query.user_id is not None:
        snapshot["userId"] = query.user_id
    if query.order_id is not None:
        snapshot["orderId"] = query.order_id
    if query.payment_key is not None:
        snapshot["paymentKey"] = query.payment_key
    if query.from_at is not None:
        snapshot["from"] = query.from_at
    if query.to_at is not None:
        snapshot["to"] = query.to_at
    if query.cursor is not None:
        snapshot["cursor"] = query.cursor
    return snapshot


def _admin_subscription_next_cursor(
    record: AdminSubscriptionListRecord,
) -> str | None:
    return encode_cursor(
        {
            "nextBillingAt": record.next_billing_at,
            "nextBillingAtNull": record.next_billing_at is None,
            "subscriptionId": record.subscription_id,
        }
    )


def _validate_admin_subscription_list_query(query: AdminListQuery) -> None:
    status_values = _admin_list_status_values(query.status)
    invalid_statuses = [
        status
        for status in status_values
        if status not in _ADMIN_SUBSCRIPTION_LIST_FILTER_STATUSES
    ]
    if invalid_statuses:
        raise BadRequestError("status is invalid")
    if (
        query.next_billing_from is not None
        and query.next_billing_to is not None
        and query.next_billing_from > query.next_billing_to
    ):
        raise BadRequestError("next billing date range is invalid")


def _admin_subscription_query_snapshot(query: AdminListQuery) -> dict[str, object]:
    snapshot: dict[str, object] = {"limit": query.limit}
    status_values = _admin_list_status_values(query.status)
    if status_values:
        snapshot["status"] = list(status_values)
    if query.user_id is not None:
        snapshot["userId"] = query.user_id
    if query.product_code is not None:
        snapshot["productCode"] = query.product_code
    if query.payment_failure is not None:
        snapshot["paymentFailure"] = query.payment_failure
    if query.next_billing_from is not None:
        snapshot["nextBillingFrom"] = query.next_billing_from
    if query.next_billing_to is not None:
        snapshot["nextBillingTo"] = query.next_billing_to
    if query.cursor is not None:
        snapshot["cursor"] = query.cursor
    return snapshot


def _admin_list_status_values(
    status: str | tuple[str, ...] | None,
) -> tuple[str, ...]:
    if status is None:
        return ()
    values = (status,) if isinstance(status, str) else status
    return tuple(
        value.strip()
        for item in values
        for value in item.split(",")
        if value.strip()
    )


def _admin_subscription_adjust_result_to_response_body(
    result: AdminSubscriptionAdjustResult,
) -> dict[str, object]:
    return {
        "subscriptionId": result.subscription_id,
        "adjustmentType": result.adjustment_type,
        "previousState": result.previous_state,
        "currentState": result.current_state,
        "operatorAuditId": result.operator_audit_id,
        "notifiedCustomer": result.notified_customer,
    }


def _admin_subscription_adjust_result_from_response_body(
    response_body: Mapping[str, object],
) -> AdminSubscriptionAdjustResult:
    previous_state = response_body.get("previousState")
    current_state = response_body.get("currentState")
    if not isinstance(previous_state, Mapping) or not isinstance(
        current_state,
        Mapping,
    ):
        raise InvalidStateTransitionError("idempotency response is invalid")
    return AdminSubscriptionAdjustResult(
        subscription_id=str(response_body["subscriptionId"]),
        adjustment_type=_subscription_adjustment_type(
            str(response_body["adjustmentType"])
        ),
        previous_state=dict(previous_state),
        current_state=dict(current_state),
        operator_audit_id=str(response_body["operatorAuditId"]),
        notified_customer=bool(response_body["notifiedCustomer"]),
    )


def _subscription_adjustment_type(value: str) -> SubscriptionAdjustmentType:
    if value == "provider_payment_sync":
        return "provider_payment_sync"
    if value == "postpone_next_billing":
        return "postpone_next_billing"
    if value == "set_next_billing_date":
        return "set_next_billing_date"
    if value == "clear_payment_failure":
        return "clear_payment_failure"
    if value == "status_override":
        return value
    raise InvalidStateTransitionError("idempotency response is invalid")


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cancelable_amount(payment: Payment) -> int:
    if payment.cancelable_amount is not None:
        return payment.cancelable_amount
    canceled_amount = sum(
        int(cancel.get("cancelAmount", 0)) for cancel in (payment.cancel_history or [])
    )
    return max(payment.amount - canceled_amount, 0)


def _validate_admin_payment_cancel_command(
    command: AdminPaymentCancelCommand,
) -> None:
    if not command.cancel_reason.strip():
        raise BadRequestError("cancelReason is required")
    if not command.reason_message.strip():
        raise BadRequestError("reasonMessage is required")


def _validate_admin_subscription_adjust_command(
    command: AdminSubscriptionAdjustCommand,
) -> None:
    if not command.reason_code.strip():
        raise BadRequestError("reasonCode is required")
    if not command.reason_message.strip():
        raise BadRequestError("reasonMessage is required")
    if (
        command.adjustment_type == "provider_payment_sync"
        and command.payment_key is None
        and command.invoice_id is None
    ):
        raise BadRequestError("paymentKey or invoiceId is required")
    if command.adjustment_type == "postpone_next_billing":
        if command.postpone_days is None or command.postpone_days < 1:
            raise BadRequestError("postponeBy.days must be positive")
        if command.postpone_days > 90:
            raise BadRequestError("postponeBy.days is too large")
    if (
        command.adjustment_type == "set_next_billing_date"
        and command.next_billing_at is None
    ):
        raise BadRequestError("nextBillingAt is required")
    if command.adjustment_type == "status_override" and command.target_status is None:
        raise BadRequestError("targetStatus is required")


def _provider_cancel_ids(cancel_history: list[dict[str, object]] | None) -> set[str]:
    ids: set[str] = set()
    for cancel in cancel_history or []:
        provider_cancel_id = cancel.get("providerCancelId") or cancel.get("cancelId")
        if isinstance(provider_cancel_id, str) and provider_cancel_id:
            ids.add(provider_cancel_id)
    return ids


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


def _payment_cancel_notification(
    notify_customer: bool,
    *,
    cancel_amount: int,
) -> dict[str, object]:
    return {
        "template": "payment_cancel_completed",
        "queued": notify_customer,
        "payload": {
            "cancelAmount": cancel_amount,
        },
    }


def _subscription_adjust_audit_state(
    current_state: dict[str, object],
    command: AdminSubscriptionAdjustCommand,
) -> dict[str, object]:
    return {
        **current_state,
        "notification": _subscription_adjust_notification(command, current_state),
    }


def _subscription_adjust_notification(
    command: AdminSubscriptionAdjustCommand,
    current_state: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "adjustmentType": command.adjustment_type,
        "status": current_state["status"],
    }
    if "nextBillingAt" in current_state:
        payload["nextBillingAt"] = current_state["nextBillingAt"]
    return {
        "template": "subscription_adjustment_completed",
        "queued": command.notify_customer,
        "payload": payload,
    }


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


def _payment_audit_state(payment: Payment) -> dict[str, object]:
    return {
        "payment_id": payment.id,
        "status": payment.status,
        "amount": payment.amount,
        "payment_key": payment.payment_key,
        "cancelable_amount": _cancelable_amount(payment),
        "cancel_history": payment.cancel_history or [],
    }


async def _apply_provider_payment_sync(
    *,
    subscription: Subscription,
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
    provider: PaymentProvider,
) -> _ProviderPaymentSyncResult:
    payment, invoice = await _load_provider_sync_payment(command, repository)
    _validate_provider_sync_ownership(
        subscription=subscription,
        payment=payment,
        invoice=invoice,
    )
    payment_key = command.payment_key or payment.payment_key
    if payment_key is None:
        raise InvalidStateTransitionError("paymentKey is required")
    provider_payment = await provider.get_payment(payment_key=payment_key)
    _validate_provider_sync_result(payment, provider_payment, payment_key)

    previous_state = _provider_sync_state(
        subscription=subscription,
        payment=payment,
        invoice=invoice,
        provider_payment_key=provider_payment.payment_key,
    )
    if provider_payment.status == "DONE":
        plan = await repository.get_admin_subscription_plan(subscription.plan_id)
        if plan is None:
            raise ResourceNotFoundError("subscription plan not found")
        _apply_provider_done_sync(
            subscription=subscription,
            payment=payment,
            invoice=invoice,
            provider_payment=provider_payment,
            plan=plan,
        )
    else:
        _apply_provider_cancel_sync(
            payment=payment,
            invoice=invoice,
            provider_payment=provider_payment,
        )
    current_state = _provider_sync_state(
        subscription=subscription,
        payment=payment,
        invoice=invoice,
        provider_payment_key=provider_payment.payment_key,
    )
    return _ProviderPaymentSyncResult(
        payment=payment,
        invoice=invoice,
        previous_state=previous_state,
        current_state=current_state,
    )


async def _load_provider_sync_payment(
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
) -> tuple[Payment, Invoice | None]:
    if command.invoice_id is not None:
        payment, invoice = await repository.get_admin_payment_by_invoice_id(
            command.invoice_id
        )
        if invoice is None:
            raise ResourceNotFoundError("invoice not found")
        if payment is None:
            raise ResourceNotFoundError("payment not found")
        return (payment, invoice)
    if command.payment_key is None:
        raise InvalidStateTransitionError("paymentKey or invoiceId is required")
    payment = await repository.get_admin_payment_by_payment_key(command.payment_key)
    if payment is None:
        raise ResourceNotFoundError("payment not found")
    return (payment, await repository.get_admin_invoice_by_payment_id(payment.id))


def _validate_provider_sync_ownership(
    *,
    subscription: Subscription,
    payment: Payment,
    invoice: Invoice | None,
) -> None:
    if payment.subscription_id != subscription.id:
        raise ProviderError("provider payment ownership mismatch")
    if invoice is not None and invoice.subscription_id != subscription.id:
        raise ProviderError("provider invoice ownership mismatch")


def _validate_provider_sync_result(
    payment: Payment,
    provider_payment: PaymentLookupProviderResult,
    requested_payment_key: str,
) -> None:
    if provider_payment.status not in {"DONE", "CANCELED", "PARTIAL_CANCELED"}:
        raise ProviderError("provider payment status is not syncable")
    if provider_payment.payment_key != requested_payment_key:
        raise ProviderError("provider payment key mismatch")
    if provider_payment.order_id != payment.order_id:
        raise ProviderError("provider payment order mismatch")
    if provider_payment.total_amount != payment.amount:
        raise ProviderError("provider payment amount mismatch")


def _apply_provider_done_sync(
    *,
    subscription: Subscription,
    payment: Payment,
    invoice: Invoice | None,
    provider_payment: PaymentLookupProviderResult,
    plan: SubscriptionPlan,
) -> None:
    if subscription.status not in {"pending", "active", "past_due"}:
        raise InvalidStateTransitionError("subscription status cannot be synced")
    if provider_payment.approved_at is None:
        raise ProviderError("provider approvedAt is required")
    payment.status = "paid"
    payment.payment_key = provider_payment.payment_key
    payment.approved_at = provider_payment.approved_at
    payment.receipt_url = provider_payment.receipt_url
    payment.method = provider_payment.method
    payment.method_detail = provider_payment.method_detail
    payment.provider_response_summary = provider_payment.response_summary
    payment.cancelable_amount = provider_payment.cancelable_amount or payment.amount
    payment.failure = None
    if invoice is not None:
        invoice.status = "paid"
        invoice.receipt_url = provider_payment.receipt_url or invoice.receipt_url
    next_billing_at = provider_payment.approved_at + timedelta(
        days=_billing_period_days(plan.billing_period)
    )
    subscription.status = "active"
    subscription.cancel_at_period_end = False
    subscription.current_period_start_at = provider_payment.approved_at
    subscription.current_period_end_at = next_billing_at
    subscription.next_billing_at = next_billing_at


def _apply_provider_cancel_sync(
    *,
    payment: Payment,
    invoice: Invoice | None,
    provider_payment: PaymentLookupProviderResult,
) -> None:
    if provider_payment.status == "CANCELED":
        payment.status = "canceled"
        if invoice is not None and invoice.status in {"issued", "paid"}:
            invoice.status = "refunded"
    else:
        payment.status = "partial_canceled"
    payment.payment_key = provider_payment.payment_key
    payment.cancelable_amount = provider_payment.cancelable_amount
    payment.provider_response_summary = provider_payment.response_summary


def _provider_sync_state(
    *,
    subscription: Subscription,
    payment: Payment,
    invoice: Invoice | None,
    provider_payment_key: str | None,
) -> dict[str, object]:
    state = {
        **_subscription_state(subscription),
        "paymentId": payment.id,
        "paymentStatus": payment.status,
        "paymentKey": payment.payment_key,
        "providerPaymentKey": provider_payment_key,
    }
    if invoice is not None:
        state["invoiceId"] = invoice.id
        state["invoiceStatus"] = invoice.status
    return state


def _billing_period_days(billing_period: str) -> int:
    return 365 if billing_period == "yearly" else 30


async def _apply_clear_payment_failure(
    *,
    subscription: Subscription,
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
) -> _PaymentFailureClearResult:
    target_status = command.target_status or "active"
    if subscription.status != "past_due":
        raise InvalidStateTransitionError("subscription has no payment failure")
    if target_status not in {"active", "cancel_scheduled"}:
        raise InvalidStateTransitionError("targetStatus is invalid")
    payment, invoice = await _load_payment_failure(command, repository, subscription.id)
    previous_state = _payment_failure_state(
        subscription=subscription,
        payment=payment,
        invoice=invoice,
    )
    payment.retry_scheduled_at = None
    payment.failure = None
    payment.status = "paid"
    if invoice is not None:
        invoice.status = "paid"
    subscription.status = target_status
    current_state = _payment_failure_state(
        subscription=subscription,
        payment=payment,
        invoice=invoice,
    )
    return _PaymentFailureClearResult(
        payment=payment,
        invoice=invoice,
        previous_state=previous_state,
        current_state=current_state,
    )


async def _load_payment_failure(
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
    subscription_id: str,
) -> tuple[Payment, Invoice | None]:
    if command.invoice_id is not None:
        payment, invoice = await repository.get_admin_payment_by_invoice_id(
            command.invoice_id
        )
        if invoice is None:
            raise ResourceNotFoundError("invoice not found")
        if payment is None:
            raise ResourceNotFoundError("payment not found")
    else:
        payment, invoice = (
            await repository.get_admin_latest_failed_subscription_payment(
                subscription_id
            )
        )
    if payment is None:
        raise InvalidStateTransitionError("failed payment is required")
    if payment.subscription_id != subscription_id:
        raise InvalidStateTransitionError("payment does not belong to subscription")
    if invoice is not None and invoice.subscription_id != subscription_id:
        raise InvalidStateTransitionError("invoice does not belong to subscription")
    if payment.status != "failed":
        raise InvalidStateTransitionError("failed payment is required")
    return (payment, invoice)


def _payment_failure_state(
    *,
    subscription: Subscription,
    payment: Payment,
    invoice: Invoice | None,
) -> dict[str, object]:
    state = {
        **_subscription_state(subscription),
        "paymentId": payment.id,
        "paymentStatus": payment.status,
        "retryAt": payment.retry_scheduled_at,
        "paymentFailureReason": payment.failure,
    }
    if invoice is not None:
        state["invoiceId"] = invoice.id
        state["invoiceStatus"] = invoice.status
    return state


async def _save_failed_subscription_adjustment_audit_record(
    *,
    audit_id: str,
    context: AdminRequestContext,
    command: AdminSubscriptionAdjustCommand,
    repository: AdminOperationsRepository,
    subscription: Subscription,
    previous_state: dict[str, object],
    message: str,
) -> None:
    await repository.save_subscription_adjustment_audit_record(
        audit_id=audit_id,
        subscription_id=subscription.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        adjustment_type=command.adjustment_type,
        reason_code=command.reason_code,
        reason_message=command.reason_message,
        previous=previous_state,
        next_value={**previous_state, "failure": {"message": message}},
        notified_customer=False,
        request_ip=context.request_ip,
        result="failed",
    )


def _apply_subscription_adjustment(
    subscription: Subscription,
    command: AdminSubscriptionAdjustCommand,
    now: datetime,
) -> None:
    if command.adjustment_type == "postpone_next_billing":
        _postpone_next_billing(subscription, command)
        return
    if command.adjustment_type == "set_next_billing_date":
        _set_next_billing_date(subscription, command, now)
        return
    if command.adjustment_type == "clear_payment_failure":
        raise InvalidStateTransitionError("clear payment failure is not available")
    if command.adjustment_type == "status_override":
        _override_subscription_status(subscription, command, now)
        return
    if command.adjustment_type == "provider_payment_sync":
        if command.payment_key is None and command.invoice_id is None:
            raise InvalidStateTransitionError("paymentKey or invoiceId is required")
        raise InvalidStateTransitionError("provider payment sync is not available")


def _postpone_next_billing(
    subscription: Subscription,
    command: AdminSubscriptionAdjustCommand,
) -> None:
    if subscription.status not in {"active", "past_due"}:
        raise InvalidStateTransitionError("subscription status cannot be postponed")
    if subscription.next_billing_at is None:
        raise InvalidStateTransitionError("next billing date is required")
    if command.postpone_days is None:
        raise BadRequestError("postponeBy.days is required")
    subscription.next_billing_at += timedelta(days=command.postpone_days)


def _set_next_billing_date(
    subscription: Subscription,
    command: AdminSubscriptionAdjustCommand,
    now: datetime,
) -> None:
    if subscription.status not in {"active", "past_due"}:
        raise InvalidStateTransitionError(
            "subscription status cannot set next billing date"
        )
    if command.next_billing_at is None:
        raise BadRequestError("nextBillingAt is required")
    if command.next_billing_at <= now:
        raise BadRequestError("nextBillingAt must be in the future")
    if command.next_billing_at > now + timedelta(days=370):
        raise BadRequestError("nextBillingAt is too far in the future")
    subscription.next_billing_at = command.next_billing_at


def _override_subscription_status(
    subscription: Subscription,
    command: AdminSubscriptionAdjustCommand,
    now: datetime,
) -> None:
    if command.target_status is None:
        raise BadRequestError("targetStatus is required")
    if subscription.status != "active" or command.target_status != "canceled":
        raise InvalidStateTransitionError("targetStatus transition is invalid")
    subscription.status = command.target_status
    subscription.cancel_at_period_end = False
    subscription.canceled_at = now
    subscription.cancel_at = now
    subscription.next_billing_at = None
    subscription.access_until = now


def _subscription_state(subscription: Subscription) -> dict[str, object]:
    return {
        "status": subscription.status,
        "nextBillingAt": subscription.next_billing_at,
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


def _payment_item(record: AdminPaymentListRecord) -> AdminPaymentListItem:
    cancelable = record.status in {"paid", "partial_canceled"}
    return AdminPaymentListItem(
        payment_id=record.payment_id,
        checkout_id=record.checkout_id,
        user_id=record.user_id,
        user_email=record.user_email,
        order_id=record.order_id,
        order_name=record.order_name,
        payment_key=record.payment_key,
        status=record.status,
        amount=record.amount,
        paid_amount=record.paid_amount,
        cancelable_amount=record.cancelable_amount if cancelable else 0,
        currency=record.currency,
        approved_at=record.approved_at,
        method_summary=record.method_summary,
        detail_url=f"/admin/payments/{record.payment_id}",
        cancel_url=(
            f"/admin/payments/{record.payment_id}/cancel" if cancelable else None
        ),
    )


def _subscription_item(
    record: AdminSubscriptionListRecord,
) -> AdminSubscriptionListItem:
    adjustable = record.status in {"active", "past_due", "cancel_scheduled"}
    return AdminSubscriptionListItem(
        subscription_id=record.subscription_id,
        user_id=record.user_id,
        user_email=record.user_email,
        product_code=record.product_code,
        product_name=record.product_name,
        plan_id=record.plan_id,
        plan_name=record.plan_name,
        status=record.status,
        current_period_start_at=record.current_period_start_at,
        current_period_end_at=record.current_period_end_at,
        next_billing_at=record.next_billing_at,
        payment_failure=_admin_subscription_payment_failure(record.payment_failure),
        default_billing_method_summary=record.default_billing_method_summary,
        detail_url=f"/admin/subscriptions/{record.subscription_id}",
        adjust_url=(
            f"/admin/subscriptions/{record.subscription_id}/adjust"
            if adjustable
            else None
        ),
    )


def _admin_subscription_payment_failure(
    payment_failure: dict[str, object] | None,
) -> dict[str, object] | None:
    if payment_failure is None:
        return None
    if payment_failure.get("hasFailure") is not True:
        return payment_failure
    return {"retryAvailable": False, **payment_failure}
