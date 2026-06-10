from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class PaymentInstrument:
    id: str
    payment_customer_id: str
    provider: Literal["tosspayments"]
    billing_key: str
    billing_key_hash: str
    status: Literal["active", "revoked"]
    provider_raw: dict[str, Any] | None = None
    revoked_at: datetime | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("pinstr")
