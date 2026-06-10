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
    next_billing_at: datetime
    current_period_start_at: datetime
    current_period_end_at: datetime
    cancel_at_period_end: bool

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("sub")
