from __future__ import annotations

from typing import Protocol

from payments.domain.entities.operator_audit import OperatorAudit


class OperatorAuditRepository(Protocol):
    async def save_operator_audit(self, audit: OperatorAudit) -> None:
        raise NotImplementedError
