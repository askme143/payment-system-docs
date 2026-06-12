from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    ConflictResponseError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.ports.billing_methods import (
    BillingMethodRecord,
    BillingMethodRepository,
)
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.operator_audits import OperatorAuditRepository
from payments.application.ports.unit_of_work import (
    BillingMethodDefaultUnitOfWorkFactory,
    BillingMethodDeleteUnitOfWorkFactory,
)
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.operator_audit import OperatorAudit

BILLING_METHOD_DEFAULT_IDEMPOTENCY_SCOPE = "billing-method-default"
BILLING_METHOD_DELETE_IDEMPOTENCY_SCOPE = "billing-method-delete"


@dataclass(frozen=True, slots=True)
class BillingMethodListItem:
    billing_method_id: str
    status: str
    is_default: bool
    method: str
    card_company: str
    masked_card_number: str
    billing_key_status: str
    deletable: bool
    delete_block_reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BillingMethodList:
    default_billing_method_id: str | None
    active_subscription_count: int
    items: list[BillingMethodListItem]


@dataclass(frozen=True, slots=True)
class SetDefaultBillingMethodResult:
    billing_method_id: str
    is_default: bool
    previous_default_billing_method_id: str | None
    default_changed_at: datetime
    applies_to: str = "all_active_subscriptions"


@dataclass(frozen=True, slots=True)
class DeleteBillingMethodResult:
    billing_method_id: str
    status: str
    deleted_at: datetime
    remaining_active_method_count: int
    default_billing_method_id: str | None


async def get_user_billing_methods(
    *,
    requester: RequestContext,
    billing_methods: BillingMethodRepository,
) -> BillingMethodList:
    """현재 회원의 활성 결제수단 목록과 삭제 가능 여부를 조회합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        billing_methods: 결제수단과 활성 구독 수를 조회하는 저장소입니다.

    Returns:
        기본 결제수단 ID, 활성 구독 수, 결제수단별 표시 정보를 담은 결과입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")

    records = await billing_methods.list_active_billing_methods_for_user(
        requester.user_id
    )
    active_subscription_count = (
        await billing_methods.count_active_subscriptions_for_user(requester.user_id)
    )
    default_billing_method_id = next(
        (record.billing_method_id for record in records if record.is_default),
        None,
    )
    return BillingMethodList(
        default_billing_method_id=default_billing_method_id,
        active_subscription_count=active_subscription_count,
        items=[
            _billing_method_list_item(
                record,
                active_method_count=len(records),
                active_subscription_count=active_subscription_count,
            )
            for record in records
        ],
    )


async def set_default_billing_method(
    *,
    requester: RequestContext,
    billing_method_id: str,
    billing_methods: BillingMethodRepository,
    changed_at: datetime,
    idempotency_keys: IdempotencyKeyRepository | None = None,
    idempotency_key: str | None = None,
    billing_method_default_uow_factory: (
        BillingMethodDefaultUnitOfWorkFactory | None
    ) = None,
) -> SetDefaultBillingMethodResult:
    """회원의 공통 기본 결제수단을 변경합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        billing_method_id: 기본값으로 지정할 결제수단 ID입니다.
        billing_methods: 결제수단 조회와 기본값 변경 저장소입니다.
        changed_at: 기본값 변경 시각입니다.

    Returns:
        변경된 기본 결제수단과 이전 기본 결제수단 정보를 담은 결과입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
        ResourceNotFoundError: 대상 결제수단이 현재 회원 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 활성 빌링키가 아니라 기본값 지정이
            불가능한 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    payload = {
        "userId": requester.user_id,
        "billingMethodId": billing_method_id,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    if idempotency_keys is not None and key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            BILLING_METHOD_DEFAULT_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _set_default_result_from_response_body(existing_key.response_body)
        if existing_key is not None and existing_key.status == "processing":
            raise InvalidStateTransitionError(
                "billing method default change is processing"
            )

    method = await billing_methods.get_billing_method_for_user(
        billing_method_id,
        requester.user_id,
    )
    if method is None:
        await _raise_billing_method_lookup_error(
            billing_methods=billing_methods,
            billing_method_id=billing_method_id,
            user_id=requester.user_id,
        )
    if method.billing_key_status != "active":
        raise InvalidStateTransitionError(
            "billing method cannot be set as default"
        )
    if idempotency_keys is not None and key_hash is not None:
        await idempotency_keys.save_idempotency_key(
            _processing_idempotency_key(
                scope=BILLING_METHOD_DEFAULT_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                now=changed_at,
                resource_id=billing_method_id,
            )
        )
    if method.is_default:
        result = SetDefaultBillingMethodResult(
            billing_method_id=billing_method_id,
            is_default=True,
            previous_default_billing_method_id=billing_method_id,
            default_changed_at=changed_at,
        )
        await _save_succeeded_idempotency_key(
            idempotency_keys=idempotency_keys,
            scope=BILLING_METHOD_DEFAULT_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            now=changed_at,
            resource_id=billing_method_id,
            response_body=_set_default_result_to_response_body(result),
        )
        return result

    try:
        previous_default_id = await _set_default_billing_method_atomically(
            billing_methods=billing_methods,
            billing_method_default_uow_factory=billing_method_default_uow_factory,
            billing_method_id=billing_method_id,
            user_id=requester.user_id,
            changed_at=changed_at,
        )
    except LookupError as exc:
        raise InvalidStateTransitionError(
            "billing method cannot be set as default"
        ) from exc
    result = SetDefaultBillingMethodResult(
        billing_method_id=billing_method_id,
        is_default=True,
        previous_default_billing_method_id=previous_default_id,
        default_changed_at=changed_at,
    )
    await _save_succeeded_idempotency_key(
        idempotency_keys=idempotency_keys,
        scope=BILLING_METHOD_DEFAULT_IDEMPOTENCY_SCOPE,
        key_hash=key_hash,
        request_hash=request_hash,
        now=changed_at,
        resource_id=billing_method_id,
        response_body=_set_default_result_to_response_body(result),
    )
    return result


async def _already_deleted_billing_method_result(
    *,
    billing_method_id: str,
    billing_methods: BillingMethodRepository,
    user_id: str,
    deleted_at: datetime,
) -> DeleteBillingMethodResult:
    active_methods = await billing_methods.list_active_billing_methods_for_user(user_id)
    default_billing_method_id = next(
        (record.billing_method_id for record in active_methods if record.is_default),
        None,
    )
    return DeleteBillingMethodResult(
        billing_method_id=billing_method_id,
        status="inactive",
        deleted_at=deleted_at,
        remaining_active_method_count=len(active_methods),
        default_billing_method_id=default_billing_method_id,
    )


async def _set_default_billing_method_atomically(
    *,
    billing_methods: BillingMethodRepository,
    billing_method_default_uow_factory: (
        BillingMethodDefaultUnitOfWorkFactory | None
    ),
    billing_method_id: str,
    user_id: str,
    changed_at: datetime,
) -> str | None:
    if billing_method_default_uow_factory is None:
        return await billing_methods.set_default_billing_method_for_user(
            billing_method_id,
            user_id,
            changed_at,
        )
    async with billing_method_default_uow_factory() as uow:
        return await uow.billing_methods.set_default_billing_method_for_user(
            billing_method_id,
            user_id,
            changed_at,
        )


async def delete_billing_method(
    *,
    requester: RequestContext,
    billing_method_id: str,
    billing_methods: BillingMethodRepository,
    deleted_at: datetime,
    idempotency_keys: IdempotencyKeyRepository | None = None,
    idempotency_key: str | None = None,
    operator_audits: OperatorAuditRepository | None = None,
    billing_method_delete_uow_factory: (
        BillingMethodDeleteUnitOfWorkFactory | None
    ) = None,
) -> DeleteBillingMethodResult:
    """회원의 활성 결제수단을 비활성화합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        billing_method_id: 삭제할 결제수단 ID입니다.
        billing_methods: 결제수단 조회와 비활성화 저장소입니다.
        deleted_at: 결제수단 삭제 처리 시각입니다.

    Returns:
        비활성화된 결제수단과 남은 활성 결제수단 정보를 담은 결과입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
        ResourceNotFoundError: 대상 결제수단이 현재 회원 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 기본 결제수단이거나 구독 유지에 필요한
            마지막 결제수단이라 삭제할 수 없는 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    payload = {
        "userId": requester.user_id,
        "billingMethodId": billing_method_id,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    if idempotency_keys is not None and key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            BILLING_METHOD_DELETE_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _delete_result_from_response_body(existing_key.response_body)
        if existing_key is not None and existing_key.status == "processing":
            raise InvalidStateTransitionError("billing method deletion is processing")

    method = await billing_methods.get_billing_method_for_user(
        billing_method_id,
        requester.user_id,
    )
    if method is None:
        inactive_method = await billing_methods.get_any_billing_method_for_user(
            billing_method_id,
            requester.user_id,
        )
        if inactive_method is not None and inactive_method.status in {
            "inactive",
            "deleted",
        }:
            return await _already_deleted_billing_method_result(
                billing_method_id=billing_method_id,
                billing_methods=billing_methods,
                user_id=requester.user_id,
                deleted_at=deleted_at,
            )
        await _raise_billing_method_lookup_error(
            billing_methods=billing_methods,
            billing_method_id=billing_method_id,
            user_id=requester.user_id,
        )
    active_methods = await billing_methods.list_active_billing_methods_for_user(
        requester.user_id
    )
    active_subscription_count = (
        await billing_methods.count_active_subscriptions_for_user(requester.user_id)
    )
    delete_block_reason = _delete_block_reason(
        method,
        active_method_count=len(active_methods),
        active_subscription_count=active_subscription_count,
    )
    if delete_block_reason is not None:
        raise ConflictResponseError(
            delete_block_reason,
            _delete_block_response_body(
                billing_method_id=billing_method_id,
                block_reason=delete_block_reason,
            ),
        )
    processing_key = (
        _processing_idempotency_key(
            scope=BILLING_METHOD_DELETE_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            now=deleted_at,
            resource_id=billing_method_id,
        )
        if key_hash is not None
        else None
    )
    remaining_methods = [
        record
        for record in active_methods
        if record.billing_method_id != billing_method_id
    ]
    default_billing_method_id = next(
        (record.billing_method_id for record in remaining_methods if record.is_default),
        None,
    )
    result = DeleteBillingMethodResult(
        billing_method_id=billing_method_id,
        status="inactive",
        deleted_at=deleted_at,
        remaining_active_method_count=len(remaining_methods),
        default_billing_method_id=default_billing_method_id,
    )
    audit = _billing_method_delete_audit(
        user_id=requester.user_id,
        method=method,
        active_method_count=len(active_methods),
        active_subscription_count=active_subscription_count,
        result=result,
        request_hash=request_hash,
        key_hash=key_hash,
        processing_key=processing_key,
    )
    await _deactivate_billing_method_with_audit(
        billing_methods=billing_methods,
        idempotency_keys=idempotency_keys,
        operator_audits=operator_audits,
        billing_method_delete_uow_factory=billing_method_delete_uow_factory,
        billing_method_id=billing_method_id,
        user_id=requester.user_id,
        deleted_at=deleted_at,
        processing_key=processing_key,
        request_hash=request_hash,
        response_body=_delete_result_to_response_body(result),
        audit=audit,
    )
    return result


async def _deactivate_billing_method_with_audit(
    *,
    billing_methods: BillingMethodRepository,
    idempotency_keys: IdempotencyKeyRepository | None,
    operator_audits: OperatorAuditRepository | None,
    billing_method_delete_uow_factory: (
        BillingMethodDeleteUnitOfWorkFactory | None
    ),
    billing_method_id: str,
    user_id: str,
    deleted_at: datetime,
    processing_key: IdempotencyKey | None,
    request_hash: str,
    response_body: dict[str, object],
    audit: OperatorAudit,
) -> None:
    if billing_method_delete_uow_factory is None:
        if idempotency_keys is not None and processing_key is not None:
            await idempotency_keys.save_idempotency_key(processing_key)
        try:
            await billing_methods.deactivate_billing_method_for_user(
                billing_method_id,
                user_id,
                deleted_at,
            )
        except LookupError as exc:
            raise InvalidStateTransitionError(
                "billing method cannot be deleted"
            ) from exc
        if operator_audits is not None:
            await operator_audits.save_operator_audit(audit)
        await _save_succeeded_idempotency_key(
            idempotency_keys=idempotency_keys,
            scope=BILLING_METHOD_DELETE_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash if processing_key is not None else None,
            request_hash=request_hash,
            now=deleted_at,
            resource_id=billing_method_id,
            response_body=response_body,
        )
        return

    async with billing_method_delete_uow_factory() as uow:
        if processing_key is not None:
            await uow.idempotency_keys.save_idempotency_key(processing_key)
        try:
            await uow.billing_methods.deactivate_billing_method_for_user(
                billing_method_id,
                user_id,
                deleted_at,
            )
        except LookupError as exc:
            raise InvalidStateTransitionError(
                "billing method cannot be deleted"
            ) from exc
        await uow.operator_audits.save_operator_audit(audit)
        await _save_succeeded_idempotency_key(
            idempotency_keys=uow.idempotency_keys,
            scope=BILLING_METHOD_DELETE_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash if processing_key is not None else None,
            request_hash=request_hash,
            now=deleted_at,
            resource_id=billing_method_id,
            response_body=response_body,
        )


async def _raise_billing_method_lookup_error(
    *,
    billing_methods: BillingMethodRepository,
    billing_method_id: str,
    user_id: str,
) -> NoReturn:
    owner_id = await billing_methods.get_billing_method_owner(billing_method_id)
    if owner_id is None:
        raise ResourceNotFoundError("billing method was not found")
    if owner_id != user_id:
        raise ForbiddenError("billing method belongs to another user")
    raise InvalidStateTransitionError("billing method is not active")


def _billing_method_delete_audit(
    *,
    user_id: str,
    method: BillingMethodRecord,
    active_method_count: int,
    active_subscription_count: int,
    result: DeleteBillingMethodResult,
    request_hash: str,
    key_hash: str | None,
    processing_key: IdempotencyKey | None,
) -> OperatorAudit:
    return OperatorAudit(
        id=OperatorAudit.generate_id(),
        operator_id=user_id,
        action="billing_method.delete",
        target_type="billing_method",
        target_id=method.billing_method_id,
        previous_state={
            "billing_method_id": method.billing_method_id,
            "status": method.status,
            "is_default": method.is_default,
            "billing_key_status": method.billing_key_status,
            "active_method_count": active_method_count,
            "active_subscription_count": active_subscription_count,
        },
        next_state={
            "billing_method_id": result.billing_method_id,
            "status": result.status,
            "billing_key_status": "revoked",
            "deleted_at": result.deleted_at.isoformat(),
            "remaining_active_method_count": result.remaining_active_method_count,
            "default_billing_method_id": result.default_billing_method_id,
        },
        reason_code="user_request",
        result="succeeded",
        created_at=result.deleted_at,
        idempotency_key_id=processing_key.id if processing_key is not None else None,
        idempotency_scope=(
            BILLING_METHOD_DELETE_IDEMPOTENCY_SCOPE
            if processing_key is not None
            else None
        ),
        idempotency_key_hash=key_hash,
        idempotency_request_hash=request_hash,
    )

def _billing_method_list_item(
    record: BillingMethodRecord,
    *,
    active_method_count: int,
    active_subscription_count: int,
) -> BillingMethodListItem:
    delete_block_reason = _delete_block_reason(
        record,
        active_method_count=active_method_count,
        active_subscription_count=active_subscription_count,
    )
    return BillingMethodListItem(
        billing_method_id=record.billing_method_id,
        status=record.status,
        is_default=record.is_default,
        method=record.method,
        card_company=record.card_company,
        masked_card_number=record.masked_card_number,
        billing_key_status=record.billing_key_status,
        deletable=delete_block_reason is None,
        delete_block_reason=delete_block_reason,
        created_at=record.created_at,
    )


def _delete_block_reason(
    record: BillingMethodRecord,
    *,
    active_method_count: int,
    active_subscription_count: int,
) -> str | None:
    if record.is_default:
        return "default_method"
    if active_subscription_count > 0 and active_method_count <= 1:
        return "last_method_for_active_subscriptions"
    return None


def _delete_block_response_body(
    *,
    billing_method_id: str,
    block_reason: str,
) -> dict[str, object]:
    return {
        "billingMethodId": billing_method_id,
        "status": "active",
        "blocked": True,
        "blockReason": block_reason,
        "message": _delete_block_message(block_reason),
    }


def _delete_block_message(block_reason: str) -> str:
    if block_reason == "last_method_for_active_subscriptions":
        return (
            "활성 구독이 1개 이상 있는 회원은 "
            "공통 결제수단이 최소 1개 남아야 합니다."
        )
    if block_reason == "default_method":
        return "기본 결제수단은 먼저 다른 결제수단을 기본값으로 지정해야 합니다."
    return "결제수단을 삭제할 수 없습니다."


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
        resource_type="billing_method",
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
            resource_type="billing_method",
            resource_id=resource_id,
            response_status=200,
            response_body=response_body,
        )
    )


def _set_default_result_to_response_body(
    result: SetDefaultBillingMethodResult,
) -> dict[str, object]:
    return {
        "billingMethodId": result.billing_method_id,
        "isDefault": result.is_default,
        "previousDefaultBillingMethodId": result.previous_default_billing_method_id,
        "defaultChangedAt": result.default_changed_at,
        "appliesTo": result.applies_to,
    }


def _set_default_result_from_response_body(
    body: Mapping[str, object],
) -> SetDefaultBillingMethodResult:
    is_default = body["isDefault"]
    default_changed_at = body["defaultChangedAt"]
    previous_default = body.get("previousDefaultBillingMethodId")
    if not isinstance(is_default, bool):
        raise InvalidStateTransitionError("idempotency response isDefault is invalid")
    if not isinstance(default_changed_at, datetime):
        raise InvalidStateTransitionError(
            "idempotency response defaultChangedAt is invalid"
        )
    return SetDefaultBillingMethodResult(
        billing_method_id=str(body["billingMethodId"]),
        is_default=is_default,
        previous_default_billing_method_id=(
            str(previous_default) if previous_default is not None else None
        ),
        default_changed_at=default_changed_at,
        applies_to=str(body["appliesTo"]),
    )


def _delete_result_to_response_body(
    result: DeleteBillingMethodResult,
) -> dict[str, object]:
    return {
        "billingMethodId": result.billing_method_id,
        "status": result.status,
        "deletedAt": result.deleted_at,
        "remainingActiveMethodCount": result.remaining_active_method_count,
        "defaultBillingMethodId": result.default_billing_method_id,
    }


def _delete_result_from_response_body(
    body: Mapping[str, object],
) -> DeleteBillingMethodResult:
    deleted_at = body["deletedAt"]
    default_billing_method_id = body.get("defaultBillingMethodId")
    if not isinstance(deleted_at, datetime):
        raise InvalidStateTransitionError("idempotency response deletedAt is invalid")
    return DeleteBillingMethodResult(
        billing_method_id=str(body["billingMethodId"]),
        status=str(body["status"]),
        deleted_at=deleted_at,
        remaining_active_method_count=_response_int(
            body["remainingActiveMethodCount"]
        ),
        default_billing_method_id=(
            str(default_billing_method_id)
            if default_billing_method_id is not None
            else None
        ),
    )


def _response_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise InvalidStateTransitionError("idempotency response is invalid")


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
