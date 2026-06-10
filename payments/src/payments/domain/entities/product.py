from dataclasses import dataclass
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class Product:
    id: str
    product_code: str
    product_type: Literal["subscription", "one_time"]
    name: str
    status: Literal["draft", "active", "paused", "archived"]

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("product")

    @classmethod
    def generate_product_id(cls) -> str:
        return cls.generate_id()
