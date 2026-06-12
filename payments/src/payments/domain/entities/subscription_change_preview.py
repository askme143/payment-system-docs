from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class SubscriptionChangePreview:
    confirmation_token: str
    subscription_id: str
    user_id: str
    product_code: str
    current_plan_id: str
    target_plan_id: str
    server_decision: Literal["upgrade", "downgrade"]
    will_apply: Literal["immediate", "next_billing_date"]
    amount: int
    currency: str
    next_billing_date: datetime | None
    expires_at: datetime
    created_at: datetime

    @classmethod
    def generate_token(cls) -> str:
        return generate_uuid_id("pct")
