from dataclasses import dataclass
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class SubscriptionPlan:
    id: str
    product_id: str
    plan_code: str
    billing_period: Literal["monthly", "yearly"]
    amount: int
    entitlements: dict
    status: Literal["draft", "active", "paused", "archived"]

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("plan")

    @classmethod
    def generate_plan_id(cls) -> str:
        return cls.generate_id()
