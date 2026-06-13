from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.cursors import decode_cursor
from payments.application.ports.operator_audits import (
    OperatorAuditQuery,
    OperatorAuditRepository,
)
from payments.domain.entities.operator_audit import OperatorAudit


class MongoOperatorAuditRepository(OperatorAuditRepository):
    def __init__(
        self,
        operator_audits: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._operator_audits = operator_audits
        self._session = session

    async def list_operator_audits(
        self,
        query: OperatorAuditQuery,
    ) -> list[OperatorAudit]:
        cursor = (
            self._operator_audits.find(_query_filter(query), session=self._session)
            .sort([("created_at", -1), ("_id", -1)])
            .limit(query.limit)
        )
        return [
            audit
            for document in [document async for document in cursor]
            if (audit := from_document(OperatorAudit, document)) is not None
        ]

    async def get_operator_audit(self, audit_id: str) -> OperatorAudit | None:
        document = await self._operator_audits.find_one(
            {"_id": audit_id},
            session=self._session,
        )
        return from_document(OperatorAudit, document)

    async def save_operator_audit(self, audit: OperatorAudit) -> None:
        await self._operator_audits.replace_one(
            {"_id": audit.id},
            to_document(audit, omit_none=True),
            upsert=True,
            session=self._session,
        )


def _query_filter(query: OperatorAuditQuery) -> dict[str, object]:
    filters: dict[str, object] = {}
    if query.operator_id is not None:
        filters["operator_id"] = query.operator_id
    if query.action is not None:
        filters["action"] = query.action
    if query.target_type is not None:
        filters["target_type"] = query.target_type
    if query.target_id is not None:
        filters["target_id"] = query.target_id
    if query.result is not None:
        filters["result"] = {"$in": list(query.result)}
    created_filter: dict[str, object] = {}
    if query.from_at is not None:
        created_filter["$gte"] = query.from_at
    if query.to_at is not None:
        created_filter["$lte"] = query.to_at
    if created_filter:
        filters["created_at"] = created_filter
    if query.cursor is not None:
        payload = decode_cursor(query.cursor)
        cursor_created_at = datetime.fromisoformat(
            str(payload["createdAt"]).replace("Z", "+00:00")
        )
        cursor_audit_id = str(payload["auditId"])
        filters["$or"] = [
            {"created_at": {"$lt": cursor_created_at}},
            {"created_at": cursor_created_at, "_id": {"$lt": cursor_audit_id}},
        ]
    return filters
