from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class PaymentCancelRequest:
    id: str
    payment_id: str
    idempotency_key_hash: str
    status: Literal["pending", "succeeded", "failed"]
    cancel_amount: int
    cancel_reason: str
    requested_by: Literal["user", "admin", "system"]
    created_at: datetime
    updated_at: datetime
    requested_user_id: str | None = None
    operator_audit_id: str | None = None
    provider_cancel_id: str | None = None
    canceled_at: datetime | None = None
    receipt_url: str | None = None
    failure: dict[str, Any] | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("pcancel")
