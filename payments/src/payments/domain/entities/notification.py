from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id

NotificationRecipientType = Literal["user", "admin", "external"]
NotificationOutboxStatus = Literal[
    "pending",
    "processing",
    "sent",
    "retry_scheduled",
    "dead_letter",
]
NotificationTemplateStatus = Literal["active", "inactive"]
JsonValue = (
    str
    | int
    | float
    | bool
    | None
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)
TemplateArgs = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class NotificationLastError:
    code: str
    message: str
    retryable: bool
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationOutboxItem:
    id: str
    idempotency_key: str
    idempotency_payload_hash: str
    event_type: str
    recipient_type: NotificationRecipientType
    recipient_user_id: str | None
    recipient_admin_id: str | None
    recipient_email: str
    product_code: str | None
    product_type: str | None
    template_key: str
    template_version: int
    template_args: TemplateArgs
    status: NotificationOutboxStatus
    attempt_count: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    locked_until_at: datetime | None = None
    worker_id: str | None = None
    provider_message_id: str | None = None
    last_error: NotificationLastError | None = None
    expires_at: datetime | None = None
    sent_at: datetime | None = None
    purge_after_at: datetime | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("nout")


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    id: str
    template_key: str
    version: int
    event_type: str
    product_code: str | None
    product_type: str | None
    status: NotificationTemplateStatus
    subject_template: str
    html_template: str
    text_template: str
    required_template_args: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("ntpl")
