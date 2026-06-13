from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from payments.application.notification_catalog import encrypted_template_args_for_event
from payments.application.ports.clock import Clock
from payments.application.ports.notifications import (
    EmailSender,
    EmailSendError,
    NotificationOutboxRepository,
    NotificationTemplateRepository,
    TemplateArgCipher,
    TemplateRenderer,
    TemplateRenderError,
)
from payments.domain.entities.notification import (
    JsonValue,
    NotificationLastError,
    NotificationOutboxItem,
    NotificationTemplate,
    TemplateArgs,
)


@dataclass(frozen=True, slots=True)
class NotificationWorkerPolicy:
    batch_size: int = 100
    claim_limit_per_run: int = 100
    poll_interval: timedelta = timedelta(seconds=10)
    lock_duration: timedelta = timedelta(minutes=5)
    max_attempts: int = 5
    backoff_schedule: tuple[timedelta, ...] = (
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=12),
    )


@dataclass(frozen=True, slots=True)
class NotificationWorkerRunSummary:
    selected_count: int
    claimed_count: int
    sent_count: int
    retry_scheduled_count: int
    dead_letter_count: int
    skipped_count: int
    failed_count: int


async def send_due_notifications(
    *,
    outbox_repository: NotificationOutboxRepository,
    template_repository: NotificationTemplateRepository,
    email_sender: EmailSender,
    template_arg_cipher: TemplateArgCipher,
    template_renderer: TemplateRenderer,
    clock: Clock,
    worker_id: str,
    policy: NotificationWorkerPolicy | None = None,
) -> NotificationWorkerRunSummary:
    """due 상태의 notification outbox item을 claim하고 이메일을 발송합니다.

    Args:
        outbox_repository: outbox claim과 상태 갱신 포트입니다.
        template_repository: 저장된 template_key/version 조회 포트입니다.
        email_sender: 렌더링된 이메일을 provider로 보내는 포트입니다.
        template_arg_cipher: encryptedArgs 복호화 포트입니다.
        template_renderer: Jinja2 렌더링 포트입니다.
        clock: worker 기준 시각을 제공하는 시간 포트입니다.
        worker_id: claim 소유자로 저장할 worker 식별자입니다.
        policy: batch, lock, retry 정책입니다. 없으면 문서 기본값을 사용합니다.

    Returns:
        선택, claim, 발송, retry, dead-letter 건수를 담은 실행 요약입니다.

    Raises:
        PaymentApplicationError: 저장소 자체 장애처럼 worker가 처리할 수 없는
            오류는 adapter에서 전파됩니다.
    """
    worker_policy = policy or NotificationWorkerPolicy()
    now = clock.utc_now()
    claimed = await outbox_repository.claim_due_notifications(
        now=now,
        lock_until=now + worker_policy.lock_duration,
        worker_id=worker_id,
        limit=min(worker_policy.batch_size, worker_policy.claim_limit_per_run),
    )
    sent_count = 0
    retry_count = 0
    dead_letter_count = 0
    failed_count = 0
    for item in claimed:
        result = await _send_claimed_notification(
            item=item,
            outbox_repository=outbox_repository,
            template_repository=template_repository,
            email_sender=email_sender,
            template_arg_cipher=template_arg_cipher,
            template_renderer=template_renderer,
            clock=clock,
            policy=worker_policy,
        )
        if result == "sent":
            sent_count += 1
        elif result == "retry_scheduled":
            retry_count += 1
        elif result == "dead_letter":
            dead_letter_count += 1
        else:
            failed_count += 1
    return NotificationWorkerRunSummary(
        selected_count=len(claimed),
        claimed_count=len(claimed),
        sent_count=sent_count,
        retry_scheduled_count=retry_count,
        dead_letter_count=dead_letter_count,
        skipped_count=0,
        failed_count=failed_count,
    )


async def _send_claimed_notification(
    *,
    item: NotificationOutboxItem,
    outbox_repository: NotificationOutboxRepository,
    template_repository: NotificationTemplateRepository,
    email_sender: EmailSender,
    template_arg_cipher: TemplateArgCipher,
    template_renderer: TemplateRenderer,
    clock: Clock,
    policy: NotificationWorkerPolicy,
) -> str:
    now = clock.utc_now()
    if item.expires_at is not None and item.expires_at <= now:
        await _mark_dead_letter(
            outbox_repository,
            item,
            code="notification_expired",
            message="notification send eligibility expired",
            occurred_at=now,
        )
        return "dead_letter"
    template = await template_repository.get_template(
        template_key=item.template_key,
        version=item.template_version,
    )
    if template is None:
        await _mark_dead_letter(
            outbox_repository,
            item,
            code="template_not_found",
            message="notification template was not found",
            occurred_at=now,
        )
        return "dead_letter"
    template_args = _validated_template_args(item, template)
    if template_args is None:
        await _mark_dead_letter(
            outbox_repository,
            item,
            code="required_template_args_missing",
            message="required template_args are missing",
            occurred_at=now,
        )
        return "dead_letter"
    decrypted_args = _decrypt_template_args(item, template_args, template_arg_cipher)
    if decrypted_args is None:
        await _mark_dead_letter(
            outbox_repository,
            item,
            code="template_arg_decrypt_failed",
            message="encrypted template_arg could not be decrypted",
            occurred_at=now,
        )
        return "dead_letter"
    try:
        rendered = template_renderer.render(
            template=template,
            template_args=decrypted_args,
        )
    except TemplateRenderError:
        await _mark_dead_letter(
            outbox_repository,
            item,
            code="template_render_failed",
            message="template render failed",
            occurred_at=now,
        )
        return "dead_letter"
    try:
        result = await email_sender.send_email(
            recipient_email=item.recipient_email,
            subject=rendered.subject,
            html_body=rendered.html_body,
            text_body=rendered.text_body,
        )
    except EmailSendError as exc:
        return await _handle_provider_error(
            outbox_repository=outbox_repository,
            item=item,
            error=exc,
            occurred_at=clock.utc_now(),
            policy=policy,
        )
    sent_at = clock.utc_now()
    await outbox_repository.mark_sent(
        item.id,
        provider_message_id=result.provider_message_id,
        sent_at=sent_at,
        purge_after_at=sent_at + timedelta(days=90),
    )
    return "sent"


def _validated_template_args(
    item: NotificationOutboxItem,
    template: NotificationTemplate,
) -> TemplateArgs | None:
    missing_args = [
        arg_name
        for arg_name in template.required_template_args
        if arg_name not in item.template_args
    ]
    if missing_args:
        return None
    return dict(item.template_args)


def _decrypt_template_args(
    item: NotificationOutboxItem,
    template_args: TemplateArgs,
    cipher: TemplateArgCipher,
) -> TemplateArgs | None:
    result = dict(template_args)
    for arg_name in encrypted_template_args_for_event(item.event_type):
        value = result.get(arg_name)
        ciphertext = _encrypted_ciphertext(value)
        if ciphertext is None:
            return None
        try:
            result[arg_name] = cipher.decrypt(ciphertext)
        except ValueError:
            return None
    return result


def _encrypted_ciphertext(value: JsonValue | None) -> str | None:
    if not isinstance(value, dict):
        return None
    if value.get("_encrypted") is not True:
        return None
    ciphertext = value.get("value")
    if not isinstance(ciphertext, str) or not ciphertext:
        return None
    return ciphertext


async def _handle_provider_error(
    *,
    outbox_repository: NotificationOutboxRepository,
    item: NotificationOutboxItem,
    error: EmailSendError,
    occurred_at: datetime,
    policy: NotificationWorkerPolicy,
) -> str:
    last_error = NotificationLastError(
        code=error.code,
        message=str(error),
        retryable=error.retryable,
        occurred_at=occurred_at,
    )
    if error.retryable and item.attempt_count < policy.max_attempts:
        await outbox_repository.schedule_retry(
            item.id,
            available_at=occurred_at + _backoff_for_attempt(item, policy),
            last_error=last_error,
        )
        return "retry_scheduled"
    await outbox_repository.mark_dead_letter(
        item.id,
        last_error=last_error,
        purge_after_at=occurred_at + timedelta(days=180),
    )
    return "dead_letter"


def _backoff_for_attempt(
    item: NotificationOutboxItem,
    policy: NotificationWorkerPolicy,
) -> timedelta:
    index = max(0, min(item.attempt_count - 1, len(policy.backoff_schedule) - 1))
    return policy.backoff_schedule[index]


async def _mark_dead_letter(
    outbox_repository: NotificationOutboxRepository,
    item: NotificationOutboxItem,
    *,
    code: str,
    message: str,
    occurred_at: datetime,
) -> None:
    await outbox_repository.mark_dead_letter(
        item.id,
        last_error=NotificationLastError(
            code=code,
            message=message,
            retryable=False,
            occurred_at=occurred_at,
        ),
        purge_after_at=occurred_at + timedelta(days=180),
    )
