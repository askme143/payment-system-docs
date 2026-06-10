from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class BillingAuth:
    id: str
    user_id: str
    payment_customer_id: str
    customer_key_snapshot: str
    set_as_default: bool
    status: Literal["ready", "issued", "failed", "expired"]
    expires_at: datetime

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("bauth")
