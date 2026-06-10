from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class Invoice:
    id: str
    user_id: str
    payment_id: str
    status: Literal["issued", "paid", "voided", "refunded"]
    issued_at: datetime
    subscription_id: str | None = None
    billing_cycle_key: str | None = None
    receipt_url: str | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("inv")
