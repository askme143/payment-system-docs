from dataclasses import dataclass
from typing import Any, Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class WebhookEvent:
    id: str
    provider: str
    event_id: str
    payload: dict[str, Any]
    status: Literal["received", "processed", "failed", "ignored"]

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("wh")
