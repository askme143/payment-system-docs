from dataclasses import dataclass
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class OneTimeSku:
    id: str
    product_id: str
    sku_code: str
    amount: int
    stock_policy: Literal["unlimited", "limited"]
    status: Literal["draft", "active", "paused", "archived"]
    purchase_limit: dict | None = None
    total_stock: int | None = None
    reserved_stock: int | None = None
    sold_stock: int | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("sku")

    @property
    def available_stock(self) -> int | None:
        if self.stock_policy == "unlimited":
            return None
        if (
            self.total_stock is None
            or self.reserved_stock is None
            or self.sold_stock is None
        ):
            return None
        return self.total_stock - self.reserved_stock - self.sold_stock
