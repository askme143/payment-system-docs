from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class PaymentCustomer:
    id: str
    user_id: str
    provider: Literal["tosspayments"]
    customer_key: str
    status: Literal["active", "revoked"]
    revoked_at: datetime | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("pcus")

    @classmethod
    def generate_pcus_id(cls) -> str:
        return cls.generate_id()

    @classmethod
    def generate_pcus_key(cls) -> str:
        return generate_uuid_id("pcus_key")
