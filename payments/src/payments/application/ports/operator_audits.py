from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from payments.domain.entities.operator_audit import OperatorAudit


@dataclass(frozen=True, slots=True)
class OperatorAuditQuery:
    operator_id: str | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    result: tuple[str, ...] | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    cursor: str | None = None
    limit: int = 50


class OperatorAuditRepository(Protocol):
    async def list_operator_audits(
        self,
        query: OperatorAuditQuery,
    ) -> list[OperatorAudit]:
        raise NotImplementedError

    async def get_operator_audit(self, audit_id: str) -> OperatorAudit | None:
        raise NotImplementedError

    async def save_operator_audit(self, audit: OperatorAudit) -> None:
        raise NotImplementedError
