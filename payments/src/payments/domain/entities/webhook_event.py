from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class WebhookEvent:
    id: str
    provider: str
    event_id: str
    status: Literal["received", "processed", "failed", "ignored"]
    payload: dict[str, object]
    event_type: str = "PAYMENT_STATUS_CHANGED"
    payment_key: str | None = None
    order_id: str | None = None
    received_at: datetime | None = None
    processed_at: datetime | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("wh")
