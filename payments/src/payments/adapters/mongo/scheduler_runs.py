from __future__ import annotations

from datetime import date, datetime
from typing import cast

from motor.motor_asyncio import AsyncIOMotorCollection

from payments.application.cursors import decode_cursor
from payments.application.ports.scheduler_runs import (
    SchedulerRunLogRepository,
    SchedulerRunQuery,
)
from payments.domain.entities.scheduler_run import (
    SchedulerJobType,
    SchedulerRunLog,
    SchedulerRunStatus,
    SchedulerTriggerSource,
)


class MongoSchedulerRunLogRepository(SchedulerRunLogRepository):
    def __init__(self, scheduler_run_logs: AsyncIOMotorCollection) -> None:
        self._scheduler_run_logs = scheduler_run_logs

    async def list_scheduler_runs(
        self,
        query: SchedulerRunQuery,
    ) -> list[SchedulerRunLog]:
        filters = _query_filter(query)
        cursor = (
            self._scheduler_run_logs.find(filters)
            .sort([("started_at", -1), ("_id", -1)])
            .limit(query.limit)
        )
        return [_from_document(document) async for document in cursor]

    async def get_scheduler_run(self, run_id: str) -> SchedulerRunLog | None:
        document = await self._scheduler_run_logs.find_one({"_id": run_id})
        if document is None:
            return None
        return _from_document(document)

    async def save_scheduler_run(self, run: SchedulerRunLog) -> None:
        await self._scheduler_run_logs.replace_one(
            {"_id": run.id},
            _to_document(run),
            upsert=True,
        )


def _query_filter(query: SchedulerRunQuery) -> dict[str, object]:
    filters: dict[str, object] = {}
    if query.job_type is not None:
        filters["job_type"] = {"$in": list(query.job_type)}
    if query.status is not None:
        filters["status"] = {"$in": list(query.status)}
    if query.trigger_source is not None:
        filters["trigger_source"] = {"$in": list(query.trigger_source)}
    if query.worker_id is not None:
        filters["worker_id"] = query.worker_id
    range_filter: dict[str, object] = {}
    if query.from_at is not None:
        range_filter["$gte"] = query.from_at
    if query.to_at is not None:
        range_filter["$lte"] = query.to_at
    if range_filter:
        filters["started_at"] = range_filter
    if query.cursor is not None:
        payload = decode_cursor(query.cursor)
        cursor_started_at = datetime.fromisoformat(
            str(payload["startedAt"]).replace("Z", "+00:00")
        )
        cursor_run_id = str(payload["runId"])
        filters["$or"] = [
            {"started_at": {"$lt": cursor_started_at}},
            {"started_at": cursor_started_at, "_id": {"$lt": cursor_run_id}},
        ]
    return filters


def _to_document(run: SchedulerRunLog) -> dict[str, object]:
    return {
        "_id": run.id,
        "job_type": run.job_type,
        "status": run.status,
        "trigger_source": run.trigger_source,
        "worker_id": run.worker_id,
        "batch_size": run.batch_size,
        "billing_date": run.billing_date.isoformat()
        if run.billing_date is not None
        else None,
        "dry_run": run.dry_run,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": run.duration_ms,
        "summary": run.summary,
        "error": run.error,
        "idempotency_key_id": run.idempotency_key_id,
        "operator_audit_id": run.operator_audit_id,
        "request_id": run.request_id,
        "exit_code": run.exit_code,
        "metadata": run.metadata,
    }


def _from_document(document: dict[str, object]) -> SchedulerRunLog:
    billing_date_value = document.get("billing_date")
    return SchedulerRunLog(
        id=str(document["_id"]),
        job_type=cast(SchedulerJobType, document["job_type"]),
        status=cast(SchedulerRunStatus, document["status"]),
        trigger_source=cast(SchedulerTriggerSource, document["trigger_source"]),
        worker_id=str(document["worker_id"]),
        batch_size=_optional_int(document["batch_size"]),
        billing_date=(
            date.fromisoformat(str(billing_date_value))
            if billing_date_value is not None
            else None
        ),
        dry_run=bool(document["dry_run"]),
        started_at=cast(datetime, document["started_at"]),
        finished_at=cast(datetime | None, document.get("finished_at")),
        duration_ms=cast(int | None, document.get("duration_ms")),
        summary=_int_dict(document.get("summary")),
        error=_object_dict(document.get("error")),
        idempotency_key_id=(
            str(document["idempotency_key_id"])
            if document.get("idempotency_key_id") is not None
            else None
        ),
        operator_audit_id=(
            str(document["operator_audit_id"])
            if document.get("operator_audit_id") is not None
            else None
        ),
        request_id=(
            str(document["request_id"])
            if document.get("request_id") is not None
            else None
        ),
        exit_code=(
            _optional_int(document["exit_code"])
            if document.get("exit_code") is not None
            else None
        ),
        metadata=_object_dict(document.get("metadata")) or {},
    )


def _optional_int(value: object) -> int:
    return int(cast(int | str, value))


def _int_dict(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, int) and not isinstance(item, bool)
    }


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}
