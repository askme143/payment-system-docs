from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from payments.application.errors import BadRequestError, ResourceNotFoundError
from payments.application.notification_catalog import (
    build_seed_notification_templates,
    encrypted_template_args_for_event,
)
from payments.application.ports.clock import Clock
from payments.application.ports.notifications import (
    NotificationOutboxRepository,
    NotificationRecipientResolver,
    NotificationTemplateRepository,
    ResolvedNotificationRecipient,
    TemplateArgCipher,
)
from payments.domain.entities.notification import (
    JsonValue,
    NotificationOutboxItem,
    NotificationRecipientType,
    TemplateArgs,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnqueueNotificationCommand:
    event_type: str
    recipient_type: NotificationRecipientType
    template_args: TemplateArgs
    idempotency_key: str
    recipient_user_id: str | None = None
    recipient_admin_id: str | None = None
    product_code: str | None = None
    product_type: str | None = None
    expires_at: datetime | None = None
    purge_after_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NotificationEnqueueDependencies:
    outbox_repository: NotificationOutboxRepository
    template_repository: NotificationTemplateRepository
    recipient_resolver: NotificationRecipientResolver
    template_arg_cipher: TemplateArgCipher
    clock: Clock


async def enqueue_notification(
    *,
    command: EnqueueNotificationCommand,
    outbox_repository: NotificationOutboxRepository,
    template_repository: NotificationTemplateRepository,
    recipient_resolver: NotificationRecipientResolver,
    template_arg_cipher: TemplateArgCipher,
    clock: Clock,
) -> NotificationOutboxItem:
    """이메일 알림 intent를 notification_outbox에 멱등하게 저장합니다.

    Args:
        command: 이벤트 유형, 수신자 식별자, template_args, 멱등키입니다.
        outbox_repository: notification_outbox 저장 포트입니다.
        template_repository: 활성 템플릿 fallback 조회 포트입니다.
        recipient_resolver: 수신자 이메일과 표시 이름을 해석하는 포트입니다.
        template_arg_cipher: 민감 template_args 필드 암호화 포트입니다.
        clock: 생성 시각과 TTL 기준 시각을 제공하는 시간 포트입니다.

    Returns:
        새로 저장했거나 같은 멱등 hash로 이미 저장된 outbox item입니다.

    Raises:
        BadRequestError: 수신자 유형과 식별자 조합이 잘못된 경우 발생합니다.
        ResourceNotFoundError: 활성 템플릿을 찾을 수 없는 경우 발생합니다.
        IdempotencyConflictError: 같은 멱등키가 다른 payload에 사용된 경우
            repository에서 발생합니다.
    """
    now = clock.utc_now()
    recipient = await _resolve_recipient(command, recipient_resolver)
    template = await template_repository.resolve_active_template(
        event_type=command.event_type,
        product_code=command.product_code,
        product_type=command.product_type,
    )
    if template is None:
        raise ResourceNotFoundError("notification template was not found")

    stored_template_args = dict(command.template_args)
    if recipient.recipient_name is not None:
        stored_template_args["recipientName"] = recipient.recipient_name
    canonical_template_args = _canonical_template_args(
        command.event_type,
        stored_template_args,
    )
    stored_template_args = _encrypt_sensitive_template_args(
        command.event_type,
        stored_template_args,
        template_arg_cipher,
    )
    item = NotificationOutboxItem(
        id=NotificationOutboxItem.generate_id(),
        idempotency_key=command.idempotency_key,
        idempotency_payload_hash=_hash_payload(
            {
                "event_type": command.event_type,
                "recipient_type": command.recipient_type,
                "recipient_user_id": recipient.recipient_user_id,
                "recipient_admin_id": recipient.recipient_admin_id,
                "recipient_email": recipient.email,
                "product_code": command.product_code,
                "product_type": command.product_type,
                "template_key": template.template_key,
                "template_version": template.version,
                "template_args": canonical_template_args,
                "expires_at": _datetime_value(command.expires_at),
            }
        ),
        event_type=command.event_type,
        recipient_type=command.recipient_type,
        recipient_user_id=recipient.recipient_user_id,
        recipient_admin_id=recipient.recipient_admin_id,
        recipient_email=recipient.email,
        product_code=command.product_code,
        product_type=command.product_type,
        template_key=template.template_key,
        template_version=template.version,
        template_args=stored_template_args,
        status="pending",
        attempt_count=0,
        available_at=now,
        expires_at=command.expires_at,
        created_at=now,
        updated_at=now,
        purge_after_at=_initial_purge_after(command, now),
    )
    return await outbox_repository.enqueue_idempotently(item)


async def enqueue_user_notification_if_available(
    *,
    dependencies: NotificationEnqueueDependencies | None,
    event_type: str,
    recipient_user_id: str,
    template_args: TemplateArgs,
    idempotency_key: str,
    product_code: str | None = None,
    product_type: str | None = None,
    expires_at: datetime | None = None,
) -> bool:
    """회원 이메일 알림을 outbox에 저장하고, 실패해도 업무 흐름을 깨지 않습니다."""
    if dependencies is None:
        return False
    try:
        await enqueue_notification(
            command=EnqueueNotificationCommand(
                event_type=event_type,
                recipient_type="user",
                recipient_user_id=recipient_user_id,
                product_code=product_code,
                product_type=product_type,
                template_args=template_args,
                idempotency_key=idempotency_key,
                expires_at=expires_at,
            ),
            outbox_repository=dependencies.outbox_repository,
            template_repository=dependencies.template_repository,
            recipient_resolver=dependencies.recipient_resolver,
            template_arg_cipher=dependencies.template_arg_cipher,
            clock=dependencies.clock,
        )
        return True
    except Exception:  # noqa: BLE001 - 알림 적재 실패는 결제 상태를 되돌리지 않습니다.
        logger.warning(
            "notification_enqueue_skipped",
            extra={
                "notification_event_type": event_type,
                "notification_idempotency_key": idempotency_key,
                "notification_recipient_user_id": recipient_user_id,
            },
            exc_info=True,
        )
        return False


async def seed_notification_templates_if_empty(
    *,
    template_repository: NotificationTemplateRepository,
    clock: Clock,
) -> int:
    """초기 notification_templates가 비어 있을 때 기본 템플릿을 생성합니다.

    Args:
        template_repository: 템플릿 조회와 저장 포트입니다.
        clock: seed 생성 시각을 제공하는 시간 포트입니다.

    Returns:
        새로 생성한 템플릿 개수입니다.

    Raises:
        PaymentApplicationError: 저장소 오류가 발생하면 adapter 오류가 전파됩니다.
    """
    if await template_repository.count_templates() > 0:
        return 0
    templates = build_seed_notification_templates(clock.utc_now())
    for template in templates:
        await template_repository.save_template(template)
    return len(templates)


async def _resolve_recipient(
    command: EnqueueNotificationCommand,
    recipient_resolver: NotificationRecipientResolver,
) -> ResolvedNotificationRecipient:
    if command.recipient_type == "user":
        if command.recipient_user_id is None:
            raise BadRequestError("recipient_user_id is required")
        recipient = await recipient_resolver.resolve_user(command.recipient_user_id)
    elif command.recipient_type == "admin":
        if command.recipient_admin_id is None:
            raise BadRequestError("recipient_admin_id is required")
        recipient = await recipient_resolver.resolve_admin(command.recipient_admin_id)
    else:
        raise BadRequestError("external notification recipient is not supported")
    if recipient.recipient_type != command.recipient_type:
        raise BadRequestError("resolved recipient type does not match")
    if not recipient.email:
        raise BadRequestError("resolved recipient email is required")
    return recipient


def _encrypt_sensitive_template_args(
    event_type: str,
    template_args: TemplateArgs,
    cipher: TemplateArgCipher,
) -> TemplateArgs:
    encrypted_args = encrypted_template_args_for_event(event_type)
    if not encrypted_args:
        return template_args
    result = dict(template_args)
    for arg_name in encrypted_args:
        value = result.get(arg_name)
        if value is None:
            continue
        result[arg_name] = {
            "_encrypted": True,
            "value": cipher.encrypt(_encryption_plaintext(value)),
        }
    return result


def _canonical_template_args(
    event_type: str,
    template_args: TemplateArgs,
) -> dict[str, JsonValue]:
    encrypted_args = set(encrypted_template_args_for_event(event_type))
    result: dict[str, JsonValue] = {}
    for key, value in template_args.items():
        if key in encrypted_args:
            result[key] = {
                "_sensitive_digest": hashlib.sha256(
                    _canonical_json(value).encode("utf-8")
                ).hexdigest()
            }
        else:
            result[key] = value
    return result


def _hash_payload(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_safe_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_safe_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    return value


def _encryption_plaintext(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _initial_purge_after(
    command: EnqueueNotificationCommand,
    now: datetime,
) -> datetime | None:
    if command.purge_after_at is not None:
        return command.purge_after_at
    if command.event_type.startswith("admin_auth.") and command.expires_at is not None:
        return command.expires_at + timedelta(days=1)
    _ = now
    return None
