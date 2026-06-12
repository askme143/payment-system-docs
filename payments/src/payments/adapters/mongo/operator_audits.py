from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import to_document
from payments.application.ports.operator_audits import OperatorAuditRepository
from payments.domain.entities.operator_audit import OperatorAudit


class MongoOperatorAuditRepository(OperatorAuditRepository):
    def __init__(
        self,
        operator_audits: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._operator_audits = operator_audits
        self._session = session

    async def save_operator_audit(self, audit: OperatorAudit) -> None:
        await self._operator_audits.replace_one(
            {"_id": audit.id},
            to_document(audit, omit_none=True),
            upsert=True,
            session=self._session,
        )
