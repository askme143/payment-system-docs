from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from motor.motor_asyncio import AsyncIOMotorCollection

from payments.adapters.email import JinjaTemplateRenderer
from payments.adapters.notifications import (
    AdminAuthOutboxEmailSender,
    HttpNotificationRecipientResolver,
)
from payments.application.errors import IdempotencyConflictError
from payments.application.jobs.notifications import (
    NotificationWorkerPolicy,
    send_due_notifications,
)
from payments.application.notifications import (
    EnqueueNotificationCommand,
    enqueue_notification,
    seed_notification_templates_if_empty,
)
from payments.application.ports.notifications import (
    EmailSendError,
    EmailSendResult,
    ResolvedNotificationRecipient,
)
from payments.domain.entities.notification import (
    NotificationLastError,
    NotificationOutboxItem,
    NotificationTemplate,
)


class FakeNotificationOutboxRepository:
    def __init__(self) -> None:
        self.items: dict[str, NotificationOutboxItem] = {}
        self.claimed_ids: list[str] = []

    async def enqueue_idempotently(
        self,
        item: NotificationOutboxItem,
    ) -> NotificationOutboxItem:
        existing = self.items.get(item.idempotency_key)
        if existing is not None:
            if existing.idempotency_payload_hash != item.idempotency_payload_hash:
                raise IdempotencyConflictError(
                    "notification idempotency key was used with another payload"
                )
            return existing
        self.items[item.idempotency_key] = item
        return item

    async def claim_due_notifications(
        self,
        *,
        now: datetime,
        lock_until: datetime,
        worker_id: str,
        limit: int,
    ) -> list[NotificationOutboxItem]:
        claimed: list[NotificationOutboxItem] = []
        for item in sorted(self.items.values(), key=lambda value: value.created_at):
            if len(claimed) >= limit:
                break
            if item.status not in {"pending", "retry_scheduled"}:
                continue
            if item.available_at > now:
                continue
            if item.locked_until_at is not None and item.locked_until_at >= now:
                continue
            claimed_item = replace(
                item,
                status="processing",
                worker_id=worker_id,
                locked_until_at=lock_until,
                attempt_count=item.attempt_count + 1,
                updated_at=now,
            )
            self.items[item.idempotency_key] = claimed_item
            self.claimed_ids.append(item.id)
            claimed.append(claimed_item)
        return claimed

    async def mark_sent(
        self,
        item_id: str,
        *,
        provider_message_id: str,
        sent_at: datetime,
        purge_after_at: datetime,
    ) -> None:
        item = self._find_by_id(item_id)
        self.items[item.idempotency_key] = replace(
            item,
            status="sent",
            provider_message_id=provider_message_id,
            sent_at=sent_at,
            updated_at=sent_at,
            purge_after_at=purge_after_at,
            locked_until_at=None,
        )

    async def schedule_retry(
        self,
        item_id: str,
        *,
        available_at: datetime,
        last_error: NotificationLastError,
    ) -> None:
        item = self._find_by_id(item_id)
        self.items[item.idempotency_key] = replace(
            item,
            status="retry_scheduled",
            available_at=available_at,
            updated_at=last_error.occurred_at,
            last_error=last_error,
            locked_until_at=None,
        )

    async def mark_dead_letter(
        self,
        item_id: str,
        *,
        last_error: NotificationLastError,
        purge_after_at: datetime,
    ) -> None:
        item = self._find_by_id(item_id)
        self.items[item.idempotency_key] = replace(
            item,
            status="dead_letter",
            updated_at=last_error.occurred_at,
            last_error=last_error,
            purge_after_at=purge_after_at,
            locked_until_at=None,
        )

    def _find_by_id(self, item_id: str) -> NotificationOutboxItem:
        return next(item for item in self.items.values() if item.id == item_id)


class FakeNotificationTemplateRepository:
    def __init__(self) -> None:
        self.templates: dict[tuple[str, int], NotificationTemplate] = {}

    async def resolve_active_template(
        self,
        *,
        event_type: str,
        product_code: str | None,
        product_type: str | None,
    ) -> NotificationTemplate | None:
        candidate_keys = []
        if product_code is not None:
            candidate_keys.append(f"{product_code}.{event_type}")
        if product_type is not None:
            candidate_keys.append(f"{product_type}.{event_type}")
        candidate_keys.append(f"default.{event_type}")
        for key in candidate_keys:
            candidates = [
                template
                for (template_key, _), template in self.templates.items()
                if template_key == key
                and template.event_type == event_type
                and template.status == "active"
            ]
            if candidates:
                return max(candidates, key=lambda template: template.version)
        return None

    async def get_template(
        self,
        *,
        template_key: str,
        version: int,
    ) -> NotificationTemplate | None:
        return self.templates.get((template_key, version))

    async def count_templates(self) -> int:
        return len(self.templates)

    async def save_template(self, template: NotificationTemplate) -> None:
        self.templates[(template.template_key, template.version)] = template


class FakeRecipientResolver:
    def __init__(self) -> None:
        self.users: dict[str, ResolvedNotificationRecipient] = {}
        self.admins: dict[str, ResolvedNotificationRecipient] = {}

    async def resolve_user(self, user_id: str) -> ResolvedNotificationRecipient:
        return self.users[user_id]

    async def resolve_admin(self, admin_id: str) -> ResolvedNotificationRecipient:
        return self.admins[admin_id]


class FakeTemplateArgCipher:
    def __init__(self) -> None:
        self.encrypt_count = 0

    def encrypt(self, plaintext: str) -> str:
        self.encrypt_count += 1
        return f"encrypted-{self.encrypt_count}:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.split(":", 1)[1]


class FixedClock:
    def utc_now(self) -> datetime:
        return datetime(2026, 6, 10, 0, 0, tzinfo=UTC)


class RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []
        self.error: EmailSendError | None = None

    async def send_email(
        self,
        *,
        recipient_email: str,
        subject: str,
        html_body: str,
        text_body: str,
    ) -> EmailSendResult:
        if self.error is not None:
            raise self.error
        self.sent.append((recipient_email, subject, html_body, text_body))
        return EmailSendResult(provider_message_id="message-1")


def _login_template(now: datetime) -> NotificationTemplate:
    return NotificationTemplate(
        id="ntpl_login",
        template_key="default.admin_auth.login_link",
        version=1,
        event_type="admin_auth.login_link",
        product_code=None,
        product_type=None,
        status="active",
        subject_template="Login {{ recipientName }}",
        html_template="<a href=\"{{ loginLink }}\">login</a>",
        text_template="{{ loginLink }} expires in {{ expiresMinutes }}",
        required_template_args=["loginLink", "expiresMinutes"],
        created_at=now,
        updated_at=now,
    )


def _reset_template(now: datetime) -> NotificationTemplate:
    return NotificationTemplate(
        id="ntpl_reset",
        template_key="default.admin_auth.password_reset",
        version=1,
        event_type="admin_auth.password_reset",
        product_code=None,
        product_type=None,
        status="active",
        subject_template="Reset {{ recipientName|default('admin') }}",
        html_template="<a href=\"{{ resetLink }}\">reset</a>",
        text_template="{{ resetLink }} expires in {{ expiresMinutes }}",
        required_template_args=["resetLink", "expiresMinutes"],
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_enqueue_notification_encrypts_auth_args_and_keeps_hash_stable() -> None:
    now = FixedClock().utc_now()
    outbox = FakeNotificationOutboxRepository()
    templates = FakeNotificationTemplateRepository()
    templates.templates[("default.admin_auth.login_link", 1)] = _login_template(now)
    resolver = FakeRecipientResolver()
    resolver.admins["admin_1"] = ResolvedNotificationRecipient(
        recipient_type="admin",
        recipient_user_id=None,
        recipient_admin_id="admin_1",
        email="ops@example.com",
        recipient_name="Ops",
    )
    cipher = FakeTemplateArgCipher()
    command = EnqueueNotificationCommand(
        event_type="admin_auth.login_link",
        recipient_type="admin",
        recipient_admin_id="admin_1",
        template_args={
            "loginLink": "https://admin.example.com/login?token=secret",
            "expiresMinutes": 10,
        },
        idempotency_key="email:admin_auth.login_link:aatok_1",
        expires_at=now + timedelta(minutes=10),
    )

    first = await enqueue_notification(
        command=command,
        outbox_repository=outbox,
        template_repository=templates,
        recipient_resolver=resolver,
        template_arg_cipher=cipher,
        clock=FixedClock(),
    )
    second = await enqueue_notification(
        command=command,
        outbox_repository=outbox,
        template_repository=templates,
        recipient_resolver=resolver,
        template_arg_cipher=cipher,
        clock=FixedClock(),
    )

    assert second is first
    assert first.recipient_email == "ops@example.com"
    assert first.template_args["recipientName"] == "Ops"
    assert first.template_args["loginLink"] == {
        "_encrypted": True,
        "value": "encrypted-1:https://admin.example.com/login?token=secret",
    }
    assert cipher.encrypt_count == 2
    assert len(outbox.items) == 1


@pytest.mark.asyncio
async def test_enqueue_notification_conflicts_on_changed_payload() -> None:
    now = FixedClock().utc_now()
    outbox = FakeNotificationOutboxRepository()
    templates = FakeNotificationTemplateRepository()
    templates.templates[("default.admin_auth.login_link", 1)] = _login_template(now)
    resolver = FakeRecipientResolver()
    resolver.admins["admin_1"] = ResolvedNotificationRecipient(
        recipient_type="admin",
        recipient_user_id=None,
        recipient_admin_id="admin_1",
        email="ops@example.com",
        recipient_name=None,
    )

    kwargs = {
        "outbox_repository": outbox,
        "template_repository": templates,
        "recipient_resolver": resolver,
        "template_arg_cipher": FakeTemplateArgCipher(),
        "clock": FixedClock(),
    }
    await enqueue_notification(
        command=EnqueueNotificationCommand(
            event_type="admin_auth.login_link",
            recipient_type="admin",
            recipient_admin_id="admin_1",
            template_args={"loginLink": "https://example.com/1", "expiresMinutes": 10},
            idempotency_key="email:admin_auth.login_link:aatok_1",
        ),
        **kwargs,
    )

    with pytest.raises(IdempotencyConflictError):
        await enqueue_notification(
            command=EnqueueNotificationCommand(
                event_type="admin_auth.login_link",
                recipient_type="admin",
                recipient_admin_id="admin_1",
                template_args={
                    "loginLink": "https://example.com/changed",
                    "expiresMinutes": 10,
                },
                idempotency_key="email:admin_auth.login_link:aatok_1",
            ),
            **kwargs,
        )


@pytest.mark.asyncio
async def test_send_due_notifications_renders_and_marks_sent() -> None:
    now = FixedClock().utc_now()
    outbox = FakeNotificationOutboxRepository()
    templates = FakeNotificationTemplateRepository()
    templates.templates[("default.admin_auth.login_link", 1)] = _login_template(now)
    item = NotificationOutboxItem(
        id="nout_1",
        idempotency_key="email:admin_auth.login_link:aatok_1",
        idempotency_payload_hash="hash",
        event_type="admin_auth.login_link",
        recipient_type="admin",
        recipient_admin_id="admin_1",
        recipient_user_id=None,
        recipient_email="ops@example.com",
        product_code=None,
        product_type=None,
        template_key="default.admin_auth.login_link",
        template_version=1,
        template_args={
            "loginLink": {"_encrypted": True, "value": "encrypted:secret-link"},
            "expiresMinutes": 10,
            "recipientName": "Ops",
        },
        status="pending",
        attempt_count=0,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    outbox.items[item.idempotency_key] = item
    sender = RecordingEmailSender()

    summary = await send_due_notifications(
        outbox_repository=outbox,
        template_repository=templates,
        email_sender=sender,
        template_arg_cipher=FakeTemplateArgCipher(),
        template_renderer=JinjaTemplateRenderer(),
        clock=FixedClock(),
        worker_id="worker-1",
    )

    assert summary.selected_count == 1
    assert summary.claimed_count == 1
    assert summary.sent_count == 1
    assert outbox.items[item.idempotency_key].status == "sent"
    assert outbox.items[item.idempotency_key].provider_message_id == "message-1"
    assert sender.sent == [
        (
            "ops@example.com",
            "Login Ops",
            '<a href="secret-link">login</a>',
            "secret-link expires in 10",
        )
    ]


@pytest.mark.asyncio
async def test_send_due_notifications_schedules_retry_for_transient_error() -> None:
    now = FixedClock().utc_now()
    outbox = FakeNotificationOutboxRepository()
    templates = FakeNotificationTemplateRepository()
    templates.templates[("default.admin_auth.login_link", 1)] = _login_template(now)
    item = NotificationOutboxItem(
        id="nout_retry",
        idempotency_key="email:admin_auth.login_link:aatok_retry",
        idempotency_payload_hash="hash",
        event_type="admin_auth.login_link",
        recipient_type="admin",
        recipient_admin_id="admin_1",
        recipient_user_id=None,
        recipient_email="ops@example.com",
        product_code=None,
        product_type=None,
        template_key="default.admin_auth.login_link",
        template_version=1,
        template_args={
            "loginLink": {"_encrypted": True, "value": "encrypted:secret-link"},
            "expiresMinutes": 10,
            "recipientName": "Ops",
        },
        status="pending",
        attempt_count=0,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    outbox.items[item.idempotency_key] = item
    sender = RecordingEmailSender()
    sender.error = EmailSendError(
        "SMTP timeout",
        code="smtp_timeout",
        retryable=True,
    )

    summary = await send_due_notifications(
        outbox_repository=outbox,
        template_repository=templates,
        email_sender=sender,
        template_arg_cipher=FakeTemplateArgCipher(),
        template_renderer=JinjaTemplateRenderer(),
        clock=FixedClock(),
        worker_id="worker-1",
    )

    stored = outbox.items[item.idempotency_key]
    assert summary.retry_scheduled_count == 1
    assert stored.status == "retry_scheduled"
    assert stored.attempt_count == 1
    assert stored.available_at == now + timedelta(minutes=1)
    assert stored.last_error is not None
    assert stored.last_error.code == "smtp_timeout"


@pytest.mark.asyncio
async def test_send_due_notifications_marks_decrypt_failure_dead_letter() -> None:
    now = FixedClock().utc_now()
    outbox = FakeNotificationOutboxRepository()
    templates = FakeNotificationTemplateRepository()
    templates.templates[("default.admin_auth.login_link", 1)] = _login_template(now)
    item = NotificationOutboxItem(
        id="nout_dead",
        idempotency_key="email:admin_auth.login_link:aatok_dead",
        idempotency_payload_hash="hash",
        event_type="admin_auth.login_link",
        recipient_type="admin",
        recipient_admin_id="admin_1",
        recipient_user_id=None,
        recipient_email="ops@example.com",
        product_code=None,
        product_type=None,
        template_key="default.admin_auth.login_link",
        template_version=1,
        template_args={"loginLink": "plain secret", "expiresMinutes": 10},
        status="pending",
        attempt_count=0,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    outbox.items[item.idempotency_key] = item
    sender = RecordingEmailSender()

    summary = await send_due_notifications(
        outbox_repository=outbox,
        template_repository=templates,
        email_sender=sender,
        template_arg_cipher=FakeTemplateArgCipher(),
        template_renderer=JinjaTemplateRenderer(),
        clock=FixedClock(),
        worker_id="worker-1",
    )

    stored = outbox.items[item.idempotency_key]
    assert summary.dead_letter_count == 1
    assert stored.status == "dead_letter"
    assert stored.last_error is not None
    assert stored.last_error.code == "template_arg_decrypt_failed"
    assert sender.sent == []


def test_notification_worker_policy_uses_documented_defaults() -> None:
    policy = NotificationWorkerPolicy()

    assert policy.batch_size == 100
    assert policy.claim_limit_per_run == 100
    assert policy.poll_interval == timedelta(seconds=10)
    assert policy.lock_duration == timedelta(minutes=5)
    assert policy.max_attempts == 5
    assert policy.backoff_schedule == (
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=12),
    )


@pytest.mark.asyncio
async def test_seed_notification_templates_initializes_only_when_empty() -> None:
    templates = FakeNotificationTemplateRepository()

    created = await seed_notification_templates_if_empty(
        template_repository=templates,
        clock=FixedClock(),
    )
    skipped = await seed_notification_templates_if_empty(
        template_repository=templates,
        clock=FixedClock(),
    )

    assert created == 11
    assert skipped == 0
    assert ("default.admin_auth.login_link", 1) in templates.templates
    for template in templates.templates.values():
        assert template.status == "active"
        assert template.template_key == f"default.{template.event_type}"
        for arg_name in template.required_template_args:
            assert f"{{{{ {arg_name} }}}}" in template.html_template
            assert f"{{{{ {arg_name} }}}}" in template.text_template


@pytest.mark.asyncio
async def test_admin_auth_outbox_sender_enqueues_login_link() -> None:
    now = FixedClock().utc_now()
    outbox = FakeNotificationOutboxRepository()
    templates = FakeNotificationTemplateRepository()
    templates.templates[("default.admin_auth.login_link", 1)] = _login_template(now)
    resolver = FakeRecipientResolver()
    resolver.admins["admin_1"] = ResolvedNotificationRecipient(
        recipient_type="admin",
        recipient_user_id=None,
        recipient_admin_id="admin_1",
        email="ops@example.com",
        recipient_name="Ops",
    )
    sender = AdminAuthOutboxEmailSender(
        link_base_url="https://admin.example.com",
        outbox_repository=outbox,
        template_repository=templates,
        recipient_resolver=resolver,
        template_arg_cipher=FakeTemplateArgCipher(),
        clock=FixedClock(),
    )

    await sender.send_login_link(
        admin_id="admin_1",
        email="ops@example.com",
        recipient_name="Ops",
        auth_token_id="aatok_1",
        login_token="alt_secret",
        expires_at=now + timedelta(minutes=10),
        request_ip="203.0.113.10",
        user_agent="admin-console/login",
    )

    item = outbox.items["email:admin_auth.login_link:aatok_1"]
    assert item.recipient_email == "ops@example.com"
    assert item.expires_at == now + timedelta(minutes=10)
    assert item.template_args["loginLink"] == {
        "_encrypted": True,
        "value": (
            "encrypted-1:"
            "https://admin.example.com/admin/auth/login/confirm?loginToken=alt_secret"
        ),
    }
    assert item.template_args["expiresMinutes"] == 10
    assert item.template_args["requestIp"] == "203.0.113.10"
    assert item.template_args["userAgent"] == "admin-console/login"


@pytest.mark.asyncio
async def test_admin_auth_outbox_sender_enqueues_password_reset_link() -> None:
    now = FixedClock().utc_now()
    outbox = FakeNotificationOutboxRepository()
    templates = FakeNotificationTemplateRepository()
    templates.templates[("default.admin_auth.password_reset", 1)] = _reset_template(
        now
    )
    resolver = FakeRecipientResolver()
    resolver.admins["admin_1"] = ResolvedNotificationRecipient(
        recipient_type="admin",
        recipient_user_id=None,
        recipient_admin_id="admin_1",
        email="ops@example.com",
        recipient_name="Ops",
    )
    sender = AdminAuthOutboxEmailSender(
        link_base_url="https://admin.example.com/",
        outbox_repository=outbox,
        template_repository=templates,
        recipient_resolver=resolver,
        template_arg_cipher=FakeTemplateArgCipher(),
        clock=FixedClock(),
    )

    await sender.send_password_reset_link(
        admin_id="admin_1",
        email="ops@example.com",
        recipient_name="Ops",
        auth_token_id="aatok_reset",
        reset_token="apr_secret",
        expires_at=now + timedelta(minutes=30),
        request_ip=None,
    )

    item = outbox.items["email:admin_auth.password_reset:aatok_reset"]
    assert item.template_args["resetLink"] == {
        "_encrypted": True,
        "value": (
            "encrypted-1:"
            "https://admin.example.com/admin/auth/password-reset/confirm"
            "?resetToken=apr_secret"
        ),
    }
    assert item.template_args["expiresMinutes"] == 30
    assert "requestIp" not in item.template_args


@pytest.mark.asyncio
async def test_http_notification_recipient_resolver_reads_user_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/notification-recipients/users/user_1"
        return httpx.Response(
            200,
            json={"email": "user@example.com", "recipientName": "User"},
        )

    resolver = HttpNotificationRecipientResolver(
        recipient_api_base_url="https://member.example.com",
        admin_accounts=cast(AsyncIOMotorCollection, object()),
        transport=httpx.MockTransport(handler),
    )

    recipient = await resolver.resolve_user("user_1")

    assert recipient == ResolvedNotificationRecipient(
        recipient_type="user",
        recipient_user_id="user_1",
        recipient_admin_id=None,
        email="user@example.com",
        recipient_name="User",
    )
