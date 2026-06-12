from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class Payment:
    id: str
    order_id: str
    amount: int
    status: Literal[
        "ready", "paid", "failed", "expired", "canceled", "partial_canceled"
    ]
    created_at: datetime
    subscription_id: str | None = None
    billing_cycle_key: str | None = None
    checkout_id: str | None = None
    payment_customer_id: str | None = None
    billing_method_id: str | None = None
    payment_key: str | None = None
    approved_at: datetime | None = None
    receipt_url: str | None = None
    method: str | None = None
    method_detail: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    provider_response_summary: dict[str, Any] | None = None
    cancelable_amount: int | None = None
    cancel_history: list[dict[str, Any]] | None = None
    expires_at: datetime | None = None
    retry_scheduled_at: datetime | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("pay")

    @classmethod
    def generate_billing_cycle_key(
        cls, subscription_id: str, billing_date: datetime
    ) -> str | None:
        if billing_date.tzinfo == UTC:
            return f"{subscription_id}:{billing_date.isoformat()}"
        return None
