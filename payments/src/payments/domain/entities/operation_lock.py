from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class OperationLock:
    id: str
    lock_key: str
    owner_token: str
    fencing_token: int
    fencing_counter_key: str
    status: Literal["active", "released", "expired"]
    locked_until_at: datetime
    acquired_at: datetime
    released_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("oplock")
