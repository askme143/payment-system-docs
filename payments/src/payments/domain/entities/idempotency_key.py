from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class IdempotencyKey:
    id: str
    scope: str
    key_hash: str
    request_hash: str
    status: Literal["processing", "succeeded", "failed", "conflicted"]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    resource_type: str | None = None
    resource_id: str | None = None
    response_status: int | None = None
    response_body: dict[str, Any] | None = None
    locked_until_at: datetime | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("idem")
