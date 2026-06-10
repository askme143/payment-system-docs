from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from payments.domain.entities.ids import generate_uuid_id


@dataclass()
class OperatorAudit:
    id: str
    operator_id: str
    action: str
    target_type: str
    target_id: str
    previous_state: dict[str, Any]
    next_state: dict[str, Any]
    reason_code: str
    result: Literal["succeeded", "failed", "rejected"]
    created_at: datetime
    idempotency_key_id: str | None = None
    idempotency_scope: str | None = None
    idempotency_key_hash: str | None = None
    idempotency_request_hash: str | None = None
    reason_message: str | None = None
    request_ip: str | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("oaudit")
