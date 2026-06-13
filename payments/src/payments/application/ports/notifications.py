from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from payments.domain.entities.notification import (
    NotificationLastError,
    NotificationOutboxItem,
    NotificationRecipientType,
    NotificationTemplate,
    TemplateArgs,
)


@dataclass(frozen=True, slots=True)
class ResolvedNotificationRecipient:
    recipient_type: NotificationRecipientType
    recipient_user_id: str | None
    recipient_admin_id: str | None
    email: str
    recipient_name: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    html_body: str
    text_body: str


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    provider_message_id: str


class EmailSendError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TemplateRenderError(Exception):
    pass


class NotificationOutboxRepository(Protocol):
    async def enqueue_idempotently(
        self,
        item: NotificationOutboxItem,
    ) -> NotificationOutboxItem:
        raise NotImplementedError

    async def claim_due_notifications(
        self,
        *,
        now: datetime,
        lock_until: datetime,
        worker_id: str,
        limit: int,
    ) -> list[NotificationOutboxItem]:
        raise NotImplementedError

    async def mark_sent(
        self,
        item_id: str,
        *,
        provider_message_id: str,
        sent_at: datetime,
        purge_after_at: datetime,
    ) -> None:
        raise NotImplementedError

    async def schedule_retry(
        self,
        item_id: str,
        *,
        available_at: datetime,
        last_error: NotificationLastError,
    ) -> None:
        raise NotImplementedError

    async def mark_dead_letter(
        self,
        item_id: str,
        *,
        last_error: NotificationLastError,
        purge_after_at: datetime,
    ) -> None:
        raise NotImplementedError


class NotificationTemplateRepository(Protocol):
    async def resolve_active_template(
        self,
        *,
        event_type: str,
        product_code: str | None,
        product_type: str | None,
    ) -> NotificationTemplate | None:
        raise NotImplementedError

    async def get_template(
        self,
        *,
        template_key: str,
        version: int,
    ) -> NotificationTemplate | None:
        raise NotImplementedError

    async def count_templates(self) -> int:
        raise NotImplementedError

    async def save_template(self, template: NotificationTemplate) -> None:
        raise NotImplementedError


class NotificationRecipientResolver(Protocol):
    async def resolve_user(self, user_id: str) -> ResolvedNotificationRecipient:
        raise NotImplementedError

    async def resolve_admin(self, admin_id: str) -> ResolvedNotificationRecipient:
        raise NotImplementedError


class TemplateArgCipher(Protocol):
    def encrypt(self, plaintext: str) -> str:
        raise NotImplementedError

    def decrypt(self, ciphertext: str) -> str:
        raise NotImplementedError


class TemplateRenderer(Protocol):
    def render(
        self,
        *,
        template: NotificationTemplate,
        template_args: TemplateArgs,
    ) -> RenderedEmail:
        raise NotImplementedError


class EmailSender(Protocol):
    async def send_email(
        self,
        *,
        recipient_email: str,
        subject: str,
        html_body: str,
        text_body: str,
    ) -> EmailSendResult:
        raise NotImplementedError
