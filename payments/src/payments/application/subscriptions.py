from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.operation_locks import (
    acquire_required_operation_lock,
    release_operation_lock,
)
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.operation_locks import OperationLockRepository
from payments.application.ports.operator_audits import OperatorAuditRepository
from payments.application.ports.subscriptions import (
    DefaultBillingMethodSummary,
    SubscriptionAccountRecord,
    SubscriptionAccountRepository,
    SubscriptionStatus,
)
from payments.application.ports.unit_of_work import (
    SubscriptionCancelUnitOfWorkFactory,
    SubscriptionResumeUnitOfWorkFactory,
)
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.subscription import Subscription

SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE = "subscriptions-cancel"
SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE = "subscriptions-resume"
_HOLDING_SUBSCRIPTION_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {"pending", "active", "past_due", "cancel_scheduled"}
)
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_PATTERN = re.compile(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b")
_LONG_NUMBER_PATTERN = re.compile(r"\b(?:\d[-\s]?){9,19}\d\b")


@dataclass(frozen=True, slots=True)
class CurrentUserSubscription:
    subscription_id: str
    product_code: str
    plan_id: str
    plan_name: str
    status: SubscriptionStatus
    current_period_start_at: datetime | None
    current_period_end_at: datetime | None
    next_billing_at: datetime | None
    resume_available: bool
    resubscribe_url: str | None


@dataclass(frozen=True, slots=True)
class CurrentUserSubscriptions:
    subscriptions: list[CurrentUserSubscription]
    billing_method: DefaultBillingMethodSummary | None


@dataclass(frozen=True, slots=True)
class SubscriptionMutationResult:
    subscription_id: str
    status: SubscriptionStatus
    cancel_at: datetime | None
    current_period_end_at: datetime | None
    next_billing_at: datetime | None
    access_until: datetime | None
    resume_available: bool


async def get_current_user_subscriptions(
    *,
    requester: RequestContext,
    subscriptions: SubscriptionAccountRepository,
) -> CurrentUserSubscriptions:
    """현재 회원의 구독 목록과 기본 결제수단 표시 정보를 조회합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        subscriptions: 회원 구독과 기본 결제수단을 조회하는 저장소입니다.

    Returns:
        상품별 구독 상태와 기본 결제수단 표시 정보를 담은 결과입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")

    records = await subscriptions.list_user_subscription_records(requester.user_id)
    billing_method = await subscriptions.get_default_billing_method(requester.user_id)
    return CurrentUserSubscriptions(
        subscriptions=[
            _current_user_subscription_from_record(record)
            for record in _select_current_subscription_records(records)
        ],
        billing_method=billing_method,
    )


async def cancel_subscription_at_period_end(
    *,
    requester: RequestContext,
    subscription_id: str,
    subscriptions: SubscriptionAccountRepository,
    canceled_at: datetime,
    idempotency_keys: IdempotencyKeyRepository | None = None,
    idempotency_key: str | None = None,
    cancel_reason: str | None = None,
    feedback: str | None = None,
    operation_locks: OperationLockRepository | None = None,
    operator_audits: OperatorAuditRepository | None = None,
    subscription_cancel_uow_factory: SubscriptionCancelUnitOfWorkFactory | None = None,
) -> SubscriptionMutationResult:
    """회원 구독을 현재 이용 기간 종료 시점에 해지 예약합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        subscription_id: 해지 예약할 구독 ID입니다.
        subscriptions: 구독 조회와 상태 변경 저장소입니다.
        canceled_at: 해지 예약 요청 시각입니다.

    Returns:
        해지 예약된 구독 상태와 재개 가능 여부입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
        ResourceNotFoundError: 구독이 없는 경우 발생합니다.
        ForbiddenError: 구독이 현재 회원 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 현재 상태에서 해지 예약할 수 없는 경우 발생합니다.
    """
    user_id = _require_user_id(requester)
    payload = {
        "userId": user_id,
        "subscriptionId": subscription_id,
        "cancelReason": cancel_reason,
        "feedback": feedback,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    if idempotency_keys is not None and key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _mutation_result_from_response_body(existing_key.response_body)
        if existing_key is not None and existing_key.status == "processing":
            raise InvalidStateTransitionError("subscription cancellation is processing")

    subscription = await _get_owned_subscription(
        subscriptions,
        subscription_id,
        user_id,
    )
    if subscription.status == "cancel_scheduled":
        result = _subscription_mutation_result(subscription)
        await _save_succeeded_idempotency_key(
            idempotency_keys=idempotency_keys,
            scope=SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            now=canceled_at,
            resource_id=subscription_id,
            response_body=_mutation_result_to_response_body(result),
        )
        return result
    if subscription.status != "active":
        raise InvalidStateTransitionError("subscription cannot be canceled")
    if subscription.current_period_end_at is None:
        raise InvalidStateTransitionError("subscription has no current period end")
    processing_key = (
        _processing_idempotency_key(
            scope=SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            now=canceled_at,
            resource_id=subscription_id,
        )
        if key_hash is not None
        else None
    )

    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"subscription:{subscription_id}",
        fencing_counter_key="subscription",
        now=canceled_at,
        metadata={
            "api": SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE,
            "request_id": requester.request_id,
            "subscription_id": subscription_id,
        },
    )
    try:
        return await _schedule_subscription_cancel_with_audit(
            subscriptions=subscriptions,
            idempotency_keys=idempotency_keys,
            operator_audits=operator_audits,
            subscription_cancel_uow_factory=subscription_cancel_uow_factory,
            subscription=subscription,
            user_id=user_id,
            canceled_at=canceled_at,
            processing_key=processing_key,
            request_hash=request_hash,
            cancel_reason=cancel_reason,
            feedback=feedback,
        )
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=canceled_at,
        )


async def _schedule_subscription_cancel_with_audit(
    *,
    subscriptions: SubscriptionAccountRepository,
    idempotency_keys: IdempotencyKeyRepository | None,
    operator_audits: OperatorAuditRepository | None,
    subscription_cancel_uow_factory: SubscriptionCancelUnitOfWorkFactory | None,
    subscription: Subscription,
    user_id: str,
    canceled_at: datetime,
    processing_key: IdempotencyKey | None,
    request_hash: str,
    cancel_reason: str | None,
    feedback: str | None,
) -> SubscriptionMutationResult:
    previous_state = _subscription_cancel_previous_state(subscription)
    if subscription_cancel_uow_factory is None:
        if idempotency_keys is not None and processing_key is not None:
            await idempotency_keys.save_idempotency_key(processing_key)
        try:
            updated = await subscriptions.schedule_subscription_cancel_at_period_end(
                subscription.id,
                user_id,
                canceled_at,
            )
        except LookupError as exc:
            raise InvalidStateTransitionError(
                "subscription cannot be canceled"
            ) from exc
        result = _subscription_mutation_result(updated)
        if operator_audits is not None:
            await operator_audits.save_operator_audit(
                _subscription_cancel_audit(
                    subscription=subscription,
                    previous_state=previous_state,
                    result=result,
                    user_id=user_id,
                    canceled_at=canceled_at,
                    request_hash=request_hash,
                    processing_key=processing_key,
                    cancel_reason=cancel_reason,
                    feedback=feedback,
                )
            )
        await _save_succeeded_idempotency_key(
            idempotency_keys=idempotency_keys,
            scope=SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash if processing_key is not None else None,
            request_hash=request_hash,
            now=canceled_at,
            resource_id=subscription.id,
            response_body=_mutation_result_to_response_body(result),
        )
        return result

    async with subscription_cancel_uow_factory() as uow:
        if processing_key is not None:
            await uow.idempotency_keys.save_idempotency_key(processing_key)
        try:
            updated = await (
                uow.subscriptions.schedule_subscription_cancel_at_period_end(
                    subscription.id,
                    user_id,
                    canceled_at,
                )
            )
        except LookupError as exc:
            raise InvalidStateTransitionError(
                "subscription cannot be canceled"
            ) from exc
        result = _subscription_mutation_result(updated)
        await uow.operator_audits.save_operator_audit(
            _subscription_cancel_audit(
                subscription=subscription,
                previous_state=previous_state,
                result=result,
                user_id=user_id,
                canceled_at=canceled_at,
                request_hash=request_hash,
                processing_key=processing_key,
                cancel_reason=cancel_reason,
                feedback=feedback,
            )
        )
        await _save_succeeded_idempotency_key(
            idempotency_keys=uow.idempotency_keys,
            scope=SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash if processing_key is not None else None,
            request_hash=request_hash,
            now=canceled_at,
            resource_id=subscription.id,
            response_body=_mutation_result_to_response_body(result),
        )
        return result


def _subscription_cancel_audit(
    *,
    subscription: Subscription,
    previous_state: dict[str, object],
    result: SubscriptionMutationResult,
    user_id: str,
    canceled_at: datetime,
    request_hash: str,
    processing_key: IdempotencyKey | None,
    cancel_reason: str | None,
    feedback: str | None,
) -> OperatorAudit:
    sanitized_feedback = _sanitize_feedback(feedback)
    return OperatorAudit(
        id=OperatorAudit.generate_id(),
        operator_id=user_id,
        action="subscription.cancel_scheduled",
        target_type="subscription",
        target_id=subscription.id,
        previous_state=previous_state,
        next_state={
            "status": result.status,
            "cancel_at": result.cancel_at.isoformat()
            if result.cancel_at is not None
            else None,
            "next_billing_at": result.next_billing_at.isoformat()
            if result.next_billing_at is not None
            else None,
            "access_until": result.access_until.isoformat()
            if result.access_until is not None
            else None,
            "resume_available": result.resume_available,
            "cancel_reason": cancel_reason,
            "feedback": sanitized_feedback,
        },
        reason_code=cancel_reason or "user_request",
        result="succeeded",
        created_at=canceled_at,
        idempotency_key_id=processing_key.id if processing_key is not None else None,
        idempotency_scope=(
            SUBSCRIPTION_CANCEL_IDEMPOTENCY_SCOPE
            if processing_key is not None
            else None
        ),
        idempotency_key_hash=(
            processing_key.key_hash if processing_key is not None else None
        ),
        idempotency_request_hash=request_hash,
        reason_message=sanitized_feedback,
    )


def _subscription_cancel_previous_state(
    subscription: Subscription,
) -> dict[str, object]:
    return {
        "status": subscription.status,
        "next_billing_at": subscription.next_billing_at.isoformat()
        if subscription.next_billing_at is not None
        else None,
        "current_period_end_at": subscription.current_period_end_at.isoformat()
        if subscription.current_period_end_at is not None
        else None,
    }


def _sanitize_feedback(feedback: str | None) -> str | None:
    if feedback is None:
        return None
    sanitized = _EMAIL_PATTERN.sub("[redacted]", feedback)
    sanitized = _PHONE_PATTERN.sub("[redacted]", sanitized)
    return _LONG_NUMBER_PATTERN.sub("[redacted]", sanitized)


async def resume_subscription(
    *,
    requester: RequestContext,
    subscription_id: str,
    subscriptions: SubscriptionAccountRepository,
    now: datetime,
    idempotency_keys: IdempotencyKeyRepository | None = None,
    idempotency_key: str | None = None,
    resume_reason: str | None = None,
    operation_locks: OperationLockRepository | None = None,
    operator_audits: OperatorAuditRepository | None = None,
    subscription_resume_uow_factory: SubscriptionResumeUnitOfWorkFactory | None = None,
) -> SubscriptionMutationResult:
    """해지 예약된 회원 구독을 현재 이용 기간 종료 전 철회합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        subscription_id: 해지 예약을 철회할 구독 ID입니다.
        subscriptions: 구독 조회와 상태 변경 저장소입니다.
        now: 재개 요청 시각입니다.

    Returns:
        재개된 구독 상태와 다음 결제 예정일입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
        ResourceNotFoundError: 구독이 없거나 현재 회원 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 현재 상태에서 재개할 수 없는 경우 발생합니다.
    """
    user_id = _require_user_id(requester)
    payload = {
        "userId": user_id,
        "subscriptionId": subscription_id,
        "resumeReason": resume_reason,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    if idempotency_keys is not None and key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _mutation_result_from_response_body(existing_key.response_body)
        if existing_key is not None and existing_key.status == "processing":
            raise InvalidStateTransitionError("subscription resume is processing")

    subscription = await _get_owned_subscription(
        subscriptions,
        subscription_id,
        user_id,
    )
    if subscription.status == "active":
        result = _subscription_mutation_result(subscription)
        await _save_succeeded_idempotency_key(
            idempotency_keys=idempotency_keys,
            scope=SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            now=now,
            resource_id=subscription_id,
            response_body=_mutation_result_to_response_body(result),
        )
        return result
    if subscription.status != "cancel_scheduled":
        raise InvalidStateTransitionError("subscription cannot be resumed")
    if (
        subscription.current_period_end_at is None
        or subscription.current_period_end_at <= now
    ):
        raise InvalidStateTransitionError("subscription period already ended")
    processing_key = (
        _processing_idempotency_key(
            scope=SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            now=now,
            resource_id=subscription_id,
        )
        if key_hash is not None
        else None
    )

    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"subscription:{subscription_id}",
        fencing_counter_key="subscription",
        now=now,
        metadata={
            "api": SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE,
            "request_id": requester.request_id,
            "subscription_id": subscription_id,
        },
    )
    try:
        return await _resume_subscription_with_audit(
            subscriptions=subscriptions,
            idempotency_keys=idempotency_keys,
            operator_audits=operator_audits,
            subscription_resume_uow_factory=subscription_resume_uow_factory,
            subscription=subscription,
            user_id=user_id,
            resumed_at=now,
            processing_key=processing_key,
            request_hash=request_hash,
            resume_reason=resume_reason,
        )
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=now,
        )


async def _resume_subscription_with_audit(
    *,
    subscriptions: SubscriptionAccountRepository,
    idempotency_keys: IdempotencyKeyRepository | None,
    operator_audits: OperatorAuditRepository | None,
    subscription_resume_uow_factory: SubscriptionResumeUnitOfWorkFactory | None,
    subscription: Subscription,
    user_id: str,
    resumed_at: datetime,
    processing_key: IdempotencyKey | None,
    request_hash: str,
    resume_reason: str | None,
) -> SubscriptionMutationResult:
    previous_state = _subscription_resume_previous_state(subscription)
    if subscription_resume_uow_factory is None:
        if idempotency_keys is not None and processing_key is not None:
            await idempotency_keys.save_idempotency_key(processing_key)
        try:
            updated = await subscriptions.resume_cancel_scheduled_subscription(
                subscription.id,
                user_id,
                resumed_at,
            )
        except LookupError as exc:
            raise InvalidStateTransitionError(
                "subscription cannot be resumed"
            ) from exc
        result = _subscription_mutation_result(updated)
        if operator_audits is not None:
            await operator_audits.save_operator_audit(
                _subscription_resume_audit(
                    subscription=subscription,
                    previous_state=previous_state,
                    result=result,
                    user_id=user_id,
                    resumed_at=resumed_at,
                    request_hash=request_hash,
                    processing_key=processing_key,
                    resume_reason=resume_reason,
                )
            )
        await _save_succeeded_idempotency_key(
            idempotency_keys=idempotency_keys,
            scope=SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash if processing_key is not None else None,
            request_hash=request_hash,
            now=resumed_at,
            resource_id=subscription.id,
            response_body=_mutation_result_to_response_body(result),
        )
        return result

    async with subscription_resume_uow_factory() as uow:
        if processing_key is not None:
            await uow.idempotency_keys.save_idempotency_key(processing_key)
        try:
            updated = await uow.subscriptions.resume_cancel_scheduled_subscription(
                subscription.id,
                user_id,
                resumed_at,
            )
        except LookupError as exc:
            raise InvalidStateTransitionError(
                "subscription cannot be resumed"
            ) from exc
        result = _subscription_mutation_result(updated)
        await uow.operator_audits.save_operator_audit(
            _subscription_resume_audit(
                subscription=subscription,
                previous_state=previous_state,
                result=result,
                user_id=user_id,
                resumed_at=resumed_at,
                request_hash=request_hash,
                processing_key=processing_key,
                resume_reason=resume_reason,
            )
        )
        await _save_succeeded_idempotency_key(
            idempotency_keys=uow.idempotency_keys,
            scope=SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash if processing_key is not None else None,
            request_hash=request_hash,
            now=resumed_at,
            resource_id=subscription.id,
            response_body=_mutation_result_to_response_body(result),
        )
        return result


def _subscription_resume_audit(
    *,
    subscription: Subscription,
    previous_state: dict[str, object],
    result: SubscriptionMutationResult,
    user_id: str,
    resumed_at: datetime,
    request_hash: str,
    processing_key: IdempotencyKey | None,
    resume_reason: str | None,
) -> OperatorAudit:
    return OperatorAudit(
        id=OperatorAudit.generate_id(),
        operator_id=user_id,
        action="subscription.resume",
        target_type="subscription",
        target_id=subscription.id,
        previous_state=previous_state,
        next_state={
            "status": result.status,
            "cancel_at": result.cancel_at.isoformat()
            if result.cancel_at is not None
            else None,
            "next_billing_at": result.next_billing_at.isoformat()
            if result.next_billing_at is not None
            else None,
            "access_until": result.access_until.isoformat()
            if result.access_until is not None
            else None,
            "resume_available": result.resume_available,
            "resume_reason": resume_reason,
        },
        reason_code=resume_reason or "user_request",
        result="succeeded",
        created_at=resumed_at,
        idempotency_key_id=processing_key.id if processing_key is not None else None,
        idempotency_scope=(
            SUBSCRIPTION_RESUME_IDEMPOTENCY_SCOPE
            if processing_key is not None
            else None
        ),
        idempotency_key_hash=(
            processing_key.key_hash if processing_key is not None else None
        ),
        idempotency_request_hash=request_hash,
        reason_message=resume_reason,
    )


def _subscription_resume_previous_state(
    subscription: Subscription,
) -> dict[str, object]:
    return {
        "status": subscription.status,
        "cancel_at": subscription.cancel_at.isoformat()
        if subscription.cancel_at is not None
        else None,
        "next_billing_at": subscription.next_billing_at.isoformat()
        if subscription.next_billing_at is not None
        else None,
        "access_until": subscription.access_until.isoformat()
        if subscription.access_until is not None
        else None,
        "current_period_end_at": subscription.current_period_end_at.isoformat()
        if subscription.current_period_end_at is not None
        else None,
    }


def _current_user_subscription_from_record(
    record: SubscriptionAccountRecord,
) -> CurrentUserSubscription:
    return CurrentUserSubscription(
        subscription_id=record.subscription_id,
        product_code=record.product_code,
        plan_id=record.plan_id,
        plan_name=record.plan_name,
        status=record.status,
        current_period_start_at=record.current_period_start_at,
        current_period_end_at=record.current_period_end_at,
        next_billing_at=record.next_billing_at,
        resume_available=record.status == "cancel_scheduled",
        resubscribe_url=(
            f"/subscriptions/checkout?productCode={record.product_code}"
            if record.status == "canceled"
            else None
        ),
    )


def _select_current_subscription_records(
    records: list[SubscriptionAccountRecord],
) -> list[SubscriptionAccountRecord]:
    selected_by_product: dict[str, SubscriptionAccountRecord] = {}
    for record in records:
        selected = selected_by_product.get(record.product_code)
        if selected is None or _subscription_record_rank(record) > (
            _subscription_record_rank(selected)
        ):
            selected_by_product[record.product_code] = record
    return sorted(
        selected_by_product.values(),
        key=lambda record: (
            record.product_code,
            _subscription_record_sort_date(record),
            record.subscription_id,
        ),
    )


def _subscription_record_rank(
    record: SubscriptionAccountRecord,
) -> tuple[int, datetime]:
    holding_priority = 1 if record.status in _HOLDING_SUBSCRIPTION_STATUSES else 0
    return holding_priority, _subscription_record_sort_date(record)


def _subscription_record_sort_date(record: SubscriptionAccountRecord) -> datetime:
    return (
        record.current_period_end_at
        or record.next_billing_at
        or record.current_period_start_at
        or datetime.min.replace(tzinfo=UTC)
    )


def _require_user_id(requester: RequestContext) -> str:
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    return requester.user_id


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


def _subscription_mutation_result(
    subscription: Subscription,
) -> SubscriptionMutationResult:
    return SubscriptionMutationResult(
        subscription_id=subscription.id,
        status=subscription.status,
        cancel_at=subscription.cancel_at,
        current_period_end_at=subscription.current_period_end_at,
        next_billing_at=subscription.next_billing_at,
        access_until=subscription.access_until,
        resume_available=subscription.status == "cancel_scheduled",
    )


def _processing_idempotency_key(
    *,
    scope: str,
    key_hash: str,
    request_hash: str,
    now: datetime,
    resource_id: str,
) -> IdempotencyKey:
    return IdempotencyKey(
        id=IdempotencyKey.generate_id(),
        scope=scope,
        key_hash=key_hash,
        request_hash=request_hash,
        status="processing",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
        resource_type="subscription",
        resource_id=resource_id,
        locked_until_at=now + timedelta(minutes=5),
    )


async def _save_succeeded_idempotency_key(
    *,
    idempotency_keys: IdempotencyKeyRepository | None,
    scope: str,
    key_hash: str | None,
    request_hash: str,
    now: datetime,
    resource_id: str,
    response_body: dict[str, object],
) -> None:
    if idempotency_keys is None or key_hash is None:
        return
    existing_key = await idempotency_keys.find_idempotency_key(scope, key_hash)
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
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
            expires_at=(
                existing_key.expires_at
                if existing_key is not None
                else now + timedelta(hours=24)
            ),
            resource_type="subscription",
            resource_id=resource_id,
            response_status=200,
            response_body=response_body,
        )
    )


def _mutation_result_to_response_body(
    result: SubscriptionMutationResult,
) -> dict[str, object]:
    return {
        "subscriptionId": result.subscription_id,
        "status": result.status,
        "cancelAt": result.cancel_at,
        "currentPeriodEnd": result.current_period_end_at,
        "nextBillingDate": result.next_billing_at,
        "accessUntil": result.access_until,
        "resumeAvailable": result.resume_available,
    }


def _mutation_result_from_response_body(
    body: Mapping[str, object],
) -> SubscriptionMutationResult:
    resume_available = body["resumeAvailable"]
    if not isinstance(resume_available, bool):
        raise InvalidStateTransitionError(
            "idempotency response resumeAvailable is invalid"
        )
    return SubscriptionMutationResult(
        subscription_id=str(body["subscriptionId"]),
        status=_subscription_status(body["status"]),
        cancel_at=_optional_datetime(body.get("cancelAt"), "cancelAt"),
        current_period_end_at=_optional_datetime(
            body.get("currentPeriodEnd"),
            "currentPeriodEnd",
        ),
        next_billing_at=_optional_datetime(
            body.get("nextBillingDate"),
            "nextBillingDate",
        ),
        access_until=_optional_datetime(body.get("accessUntil"), "accessUntil"),
        resume_available=resume_available,
    )


def _subscription_status(value: object) -> SubscriptionStatus:
    if value in {"pending", "active", "past_due", "cancel_scheduled", "canceled"}:
        return cast(SubscriptionStatus, value)
    raise InvalidStateTransitionError("idempotency response status is invalid")


def _optional_datetime(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raise InvalidStateTransitionError(f"idempotency response {field} is invalid")


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
