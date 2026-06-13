from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import cast

from payments.application.admin_catalog import AdminRequestContext
from payments.application.cursors import encode_cursor
from payments.application.errors import (
    BadRequestError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.ports.clock import Clock
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.scheduler_runs import (
    SchedulerRunLogRepository,
    SchedulerRunQuery,
)
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.scheduler_run import (
    SchedulerJobType,
    SchedulerRunLog,
    SchedulerRunStatus,
    SchedulerTriggerSource,
)

ADMIN_SCHEDULER_RUN_IDEMPOTENCY_SCOPE = "admin-scheduler-run"
_JOB_TYPES: frozenset[str] = frozenset({"billing", "reminder", "cancel_expiration"})

type SchedulerJobCallable = Callable[..., Awaitable[object]]
type OperatorAuditSaver = Callable[[OperatorAudit], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class SchedulerRunPage:
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class SchedulerRunListItem:
    run_id: str
    job_type: SchedulerJobType
    status: SchedulerRunStatus
    trigger_source: SchedulerTriggerSource
    worker_id: str
    batch_size: int
    billing_date: date | None
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    summary: dict[str, int]
    operator_audit_id: str | None


@dataclass(frozen=True, slots=True)
class SchedulerRunListResult:
    items: list[SchedulerRunListItem]
    page: SchedulerRunPage


@dataclass(frozen=True, slots=True)
class SchedulerRunDetailResult:
    run_id: str
    job_type: SchedulerJobType
    status: SchedulerRunStatus
    trigger_source: SchedulerTriggerSource
    worker_id: str
    batch_size: int
    billing_date: date | None
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    summary: dict[str, int]
    error: dict[str, object] | None
    idempotency_key_id: str | None
    operator_audit_id: str | None
    request_id: str | None
    exit_code: int | None
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdminSchedulerRunCommand:
    job_type: SchedulerJobType
    billing_date: date | None
    limit: int
    dry_run: bool
    reason_code: str
    reason_message: str | None = None


@dataclass(frozen=True, slots=True)
class AdminSchedulerRunResult:
    run_id: str
    job_type: SchedulerJobType
    status: SchedulerRunStatus
    trigger_source: SchedulerTriggerSource
    summary: dict[str, int]
    operator_audit_id: str


async def list_scheduler_runs(
    query: SchedulerRunQuery,
    repository: SchedulerRunLogRepository,
) -> SchedulerRunListResult:
    """스케쥴러 실행 이력을 조회합니다.

    Args:
        query: 운영 콘솔 필터와 페이지 조건입니다.
        repository: scheduler_run_logs 조회 저장소입니다.

    Returns:
        실행 이력 목록과 커서 페이지 정보입니다.

    Raises:
        BadRequestError: 필터나 페이지 조건이 문서 계약과 맞지 않을 때 발생합니다.
    """
    _validate_scheduler_run_query(query)
    records = await repository.list_scheduler_runs(
        replace(query, limit=query.limit + 1)
    )
    page_records = records[: query.limit]
    has_more = len(records) > query.limit
    return SchedulerRunListResult(
        items=[_list_item(record) for record in page_records],
        page=SchedulerRunPage(
            next_cursor=(
                _scheduler_run_next_cursor(page_records[-1])
                if has_more and page_records
                else None
            ),
            has_more=has_more,
        ),
    )


async def get_scheduler_run_detail(
    run_id: str,
    repository: SchedulerRunLogRepository,
) -> SchedulerRunDetailResult:
    """단일 스케쥴러 실행 로그를 조회합니다."""
    run = await repository.get_scheduler_run(run_id)
    if run is None:
        raise ResourceNotFoundError("scheduler run not found")
    return _detail(run)


async def run_admin_scheduler_run(
    *,
    context: AdminRequestContext,
    command: AdminSchedulerRunCommand,
    scheduler_runs: SchedulerRunLogRepository,
    clock: Clock,
    idempotency_key: str,
    idempotency_keys: IdempotencyKeyRepository | None,
    billing_job: SchedulerJobCallable | None,
    expiration_job: SchedulerJobCallable | None,
    save_operator_audit: OperatorAuditSaver | None,
) -> AdminSchedulerRunResult:
    """운영자가 어드민 API 서버에서 스케쥴러 job 함수를 동기 실행합니다.

    Args:
        context: 인증된 관리자 요청 컨텍스트입니다.
        command: 실행할 jobType, 기준일, limit, dryRun, 사유입니다.
        scheduler_runs: scheduler_run_logs 저장소입니다.
        clock: 실행 시각을 제공하는 포트입니다.
        idempotency_key: 수동 실행 멱등키 원문입니다.
        idempotency_keys: 멱등 응답 저장소입니다.
            테스트나 제한 환경에서는 생략할 수 있습니다.
        billing_job: billing/reminder application job 호출 함수입니다.
        expiration_job: cancel_expiration application job 호출 함수입니다.
        save_operator_audit: operator_audits 저장 함수입니다.

    Returns:
        완료된 수동 실행 결과입니다.

    Raises:
        BadRequestError: 입력값이 문서 계약과 맞지 않을 때 발생합니다.
        IdempotencyConflictError: 같은 멱등키가 다른 payload에 쓰였을 때 발생합니다.
    """
    _validate_admin_scheduler_command(command)
    now = clock.utc_now()
    payload = {
        "jobType": command.job_type,
        "billingDate": command.billing_date.isoformat()
        if command.billing_date is not None
        else None,
        "limit": command.limit,
        "dryRun": command.dry_run,
        "reasonCode": command.reason_code,
        "reasonMessage": command.reason_message,
    }
    key_hash = _hash_text(idempotency_key)
    request_hash = _hash_payload(payload)
    existing_key: IdempotencyKey | None = None
    if idempotency_keys is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            ADMIN_SCHEDULER_RUN_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _admin_result_from_response_body(existing_key.response_body)
        if existing_key is not None and existing_key.status == "processing":
            raise InvalidStateTransitionError("admin scheduler run is processing")

    run = SchedulerRunLog(
        id=SchedulerRunLog.generate_id(),
        job_type=command.job_type,
        status="running",
        trigger_source="admin_manual",
        worker_id=f"{context.admin_id}:{context.request_id}",
        batch_size=command.limit,
        billing_date=command.billing_date,
        dry_run=command.dry_run,
        started_at=now,
        request_id=context.request_id,
    )
    processing_key = IdempotencyKey(
        id=(
            existing_key.id
            if existing_key is not None
            else IdempotencyKey.generate_id()
        ),
        scope=ADMIN_SCHEDULER_RUN_IDEMPOTENCY_SCOPE,
        key_hash=key_hash,
        request_hash=request_hash,
        status="processing",
        created_at=existing_key.created_at if existing_key is not None else now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
        resource_type="scheduler_run",
        resource_id=run.id,
        locked_until_at=now + timedelta(minutes=30),
    )
    if idempotency_keys is not None:
        await idempotency_keys.save_idempotency_key(processing_key)
    await scheduler_runs.save_scheduler_run(run)

    try:
        raw_summary = await _run_selected_job(command, billing_job, expiration_job)
    except Exception:
        failed_at = clock.utc_now()
        operator_audit_id = OperatorAudit.generate_id()
        run.status = "failed"
        run.finished_at = failed_at
        run.duration_ms = _duration_ms(run.started_at, failed_at)
        run.exit_code = 1
        run.operator_audit_id = operator_audit_id
        run.idempotency_key_id = processing_key.id
        run.error = {
            "code": "SCHEDULER_RUN_FAILED",
            "message": "scheduler run failed",
            "retryable": True,
        }
        await scheduler_runs.save_scheduler_run(run)
        audit = OperatorAudit(
            id=operator_audit_id,
            operator_id=context.admin_id,
            action="scheduler.run_manual",
            target_type="scheduler_run",
            target_id=run.id,
            previous_state={"status": None},
            next_state={
                "run_id": run.id,
                "job_type": run.job_type,
                "status": run.status,
                "error": run.error,
            },
            reason_code=command.reason_code,
            reason_message=command.reason_message,
            result="failed",
            request_ip=context.request_ip,
            created_at=failed_at,
            idempotency_key_id=processing_key.id,
            idempotency_scope=ADMIN_SCHEDULER_RUN_IDEMPOTENCY_SCOPE,
            idempotency_key_hash=key_hash,
            idempotency_request_hash=request_hash,
        )
        if save_operator_audit is not None:
            maybe_awaitable = save_operator_audit(audit)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        if idempotency_keys is not None:
            await idempotency_keys.save_idempotency_key(
                IdempotencyKey(
                    id=processing_key.id,
                    scope=ADMIN_SCHEDULER_RUN_IDEMPOTENCY_SCOPE,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    status="failed",
                    created_at=processing_key.created_at,
                    updated_at=failed_at,
                    expires_at=processing_key.expires_at,
                    resource_type="scheduler_run",
                    resource_id=run.id,
                    response_status=500,
                    response_body={"code": "SCHEDULER_RUN_FAILED"},
                )
            )
        raise

    finished_at = clock.utc_now()
    run.status = "succeeded"
    run.finished_at = finished_at
    run.duration_ms = _duration_ms(run.started_at, finished_at)
    run.exit_code = 0
    run.summary = _normalize_summary(command.job_type, raw_summary)
    operator_audit_id = OperatorAudit.generate_id()
    run.operator_audit_id = operator_audit_id
    run.idempotency_key_id = processing_key.id
    await scheduler_runs.save_scheduler_run(run)

    result = AdminSchedulerRunResult(
        run_id=run.id,
        job_type=run.job_type,
        status=run.status,
        trigger_source=run.trigger_source,
        summary=run.summary,
        operator_audit_id=operator_audit_id,
    )
    audit = OperatorAudit(
        id=operator_audit_id,
        operator_id=context.admin_id,
        action="scheduler.run_manual",
        target_type="scheduler_run",
        target_id=run.id,
        previous_state={"status": None},
        next_state={
            "run_id": run.id,
            "job_type": run.job_type,
            "status": run.status,
            "summary": run.summary,
        },
        reason_code=command.reason_code,
        reason_message=command.reason_message,
        result="succeeded",
        request_ip=context.request_ip,
        created_at=finished_at,
        idempotency_key_id=processing_key.id,
        idempotency_scope=ADMIN_SCHEDULER_RUN_IDEMPOTENCY_SCOPE,
        idempotency_key_hash=key_hash,
        idempotency_request_hash=request_hash,
    )
    if save_operator_audit is not None:
        maybe_awaitable = save_operator_audit(audit)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable
    if idempotency_keys is not None:
        await idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=processing_key.id,
                scope=ADMIN_SCHEDULER_RUN_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="succeeded",
                created_at=processing_key.created_at,
                updated_at=finished_at,
                expires_at=processing_key.expires_at,
                resource_type="scheduler_run",
                resource_id=run.id,
                response_status=201,
                response_body=_admin_result_to_response_body(result),
            )
        )
    return result


async def _run_selected_job(
    command: AdminSchedulerRunCommand,
    billing_job: SchedulerJobCallable | None,
    expiration_job: SchedulerJobCallable | None,
) -> object:
    if command.job_type in {"billing", "reminder"}:
        if billing_job is None:
            raise InvalidStateTransitionError("billing scheduler job is not configured")
        return await billing_job(
            job_type=command.job_type,
            billing_date=command.billing_date,
            limit=command.limit,
            dry_run=command.dry_run,
        )
    if expiration_job is None:
        raise InvalidStateTransitionError("expiration scheduler job is not configured")
    return await expiration_job(limit=command.limit, dry_run=command.dry_run)


def _validate_scheduler_run_query(query: SchedulerRunQuery) -> None:
    if query.limit < 1 or query.limit > 100:
        raise BadRequestError("limit is invalid")
    if (
        query.from_at is not None
        and query.to_at is not None
        and query.from_at > query.to_at
    ):
        raise BadRequestError("date range is invalid")


def _validate_admin_scheduler_command(command: AdminSchedulerRunCommand) -> None:
    if command.job_type not in _JOB_TYPES:
        raise BadRequestError("jobType is invalid")
    if command.limit < 1 or command.limit > 100:
        raise BadRequestError("limit is invalid")
    if not command.reason_code.strip():
        raise BadRequestError("reason is required")


def _list_item(run: SchedulerRunLog) -> SchedulerRunListItem:
    return SchedulerRunListItem(
        run_id=run.id,
        job_type=run.job_type,
        status=run.status,
        trigger_source=run.trigger_source,
        worker_id=run.worker_id,
        batch_size=run.batch_size,
        billing_date=run.billing_date,
        dry_run=run.dry_run,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        summary=dict(run.summary),
        operator_audit_id=run.operator_audit_id,
    )


def _detail(run: SchedulerRunLog) -> SchedulerRunDetailResult:
    return SchedulerRunDetailResult(
        run_id=run.id,
        job_type=run.job_type,
        status=run.status,
        trigger_source=run.trigger_source,
        worker_id=run.worker_id,
        batch_size=run.batch_size,
        billing_date=run.billing_date,
        dry_run=run.dry_run,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        summary=dict(run.summary),
        error=dict(run.error) if run.error is not None else None,
        idempotency_key_id=run.idempotency_key_id,
        operator_audit_id=run.operator_audit_id,
        request_id=run.request_id,
        exit_code=run.exit_code,
        metadata=dict(run.metadata),
    )


def _scheduler_run_next_cursor(run: SchedulerRunLog) -> str:
    return encode_cursor({"startedAt": run.started_at, "runId": run.id})


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return int((finished_at - started_at).total_seconds() * 1000)


def _normalize_summary(
    job_type: SchedulerJobType,
    raw_summary: object,
) -> dict[str, int]:
    if isinstance(raw_summary, Mapping):
        data = raw_summary
    else:
        data = {
            name: getattr(raw_summary, name)
            for name in dir(raw_summary)
            if not name.startswith("_") and not callable(getattr(raw_summary, name))
        }
    if job_type == "cancel_expiration":
        selected = _int_value(data, "selected_count")
        processed = _int_value(data, "processed_count")
        failed = _int_value(data, "failed_count")
        skipped = _int_value(data, "skipped_count")
        return {
            "selected": selected,
            "processed": processed,
            "paid": 0,
            "failed": failed,
            "skipped": skipped,
        }
    processed = _int_value(data, "processed")
    return {
        "selected": _int_value(data, "selected", processed),
        "processed": processed,
        "paid": _int_value(data, "paid"),
        "failed": _int_value(data, "failed"),
        "skipped": _int_value(data, "skipped"),
    }


def _int_value(data: Mapping[str, object], key: str, default: int = 0) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _admin_result_to_response_body(
    result: AdminSchedulerRunResult,
) -> dict[str, object]:
    return {
        "runId": result.run_id,
        "jobType": result.job_type,
        "status": result.status,
        "triggerSource": result.trigger_source,
        "summary": result.summary,
        "operatorAuditId": result.operator_audit_id,
    }


def _admin_result_from_response_body(
    body: Mapping[str, object],
) -> AdminSchedulerRunResult:
    return AdminSchedulerRunResult(
        run_id=str(body["runId"]),
        job_type=_job_type(str(body["jobType"])),
        status=_status(str(body["status"])),
        trigger_source=_trigger_source(str(body["triggerSource"])),
        summary=_summary_from_response_body(body["summary"]),
        operator_audit_id=str(body["operatorAuditId"]),
    )


def _job_type(value: str) -> SchedulerJobType:
    if value in {"billing", "reminder", "cancel_expiration"}:
        return cast(SchedulerJobType, value)
    raise InvalidStateTransitionError("scheduler run response is invalid")


def _status(value: str) -> SchedulerRunStatus:
    if value in {"running", "succeeded", "failed", "canceled"}:
        return cast(SchedulerRunStatus, value)
    raise InvalidStateTransitionError("scheduler run response is invalid")


def _trigger_source(value: str) -> SchedulerTriggerSource:
    if value in {"kubernetes_cronjob", "admin_manual", "internal_api", "cli"}:
        return cast(SchedulerTriggerSource, value)
    raise InvalidStateTransitionError("scheduler run response is invalid")


def _summary_from_response_body(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise InvalidStateTransitionError("scheduler run response is invalid")
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, int) and not isinstance(item, bool)
    }
