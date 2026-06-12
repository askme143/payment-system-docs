from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass
class Subscription:
    id: str
    user_id: str
    payment_customer_id: str
    plan_id: str
    product_code: str
    status: Literal["pending", "active", "past_due", "cancel_scheduled", "canceled"]
    cancel_at_period_end: bool
    next_billing_at: datetime | None = None
    current_period_start_at: datetime | None = None
    current_period_end_at: datetime | None = None
    cancel_at: datetime | None = None
    canceled_at: datetime | None = None
    access_until: datetime | None = None
    pending_plan_id: str | None = None
    pending_plan_effective_at: datetime | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("sub")
