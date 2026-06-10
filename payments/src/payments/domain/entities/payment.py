from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class Payment:
    id: str
    order_id: str
    amount: int
    status: Literal["ready", "paid", "failed", "canceled", "partial_canceled"]
    created_at: datetime
    subscription_id: str | None = None
    billing_cycle_key: str | None = None
    checkout_id: str | None = None
    payment_customer_id: str | None = None
    payment_key: str | None = None
    cancelable_amount: int | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("pay")

    @classmethod
    def generate_billing_cycle_key(cls, subscription_id: str, billing_date: datetime) -> str | None:
        if billing_date.tzinfo == timezone.utc:
            return f"{subscription_id}:{billing_date.isoformat()}"
        return None
