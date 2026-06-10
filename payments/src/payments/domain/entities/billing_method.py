from dataclasses import dataclass
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class BillingMethod:
    id: str
    user_id: str
    payment_customer_id: str
    instrument_id: str
    display_name: str
    provider: Literal["tosspayments"]
    is_default: bool
    status: Literal["active", "inactive", "deleted"]
    masked_number: str | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("bm")
