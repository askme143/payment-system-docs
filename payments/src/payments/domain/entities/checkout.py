from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class Checkout:
    id: str
    user_id: str
    payment_customer_id: str
    items: list[dict]
    status: Literal["ready", "paid", "failed", "expired"]
    created_at: datetime
    last_payment_id: str | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("chk")
