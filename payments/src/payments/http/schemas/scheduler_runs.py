from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from payments.application.scheduler_runs import (
    AdminSchedulerRunResult,
    SchedulerRunDetailResult,
    SchedulerRunListItem,
    SchedulerRunListResult,
)


class SchedulerRunPageResponse(BaseModel):
    next_cursor: str | None = Field(alias="nextCursor")
    has_more: bool = Field(alias="hasMore")


class SchedulerRunSummaryResponse(BaseModel):
    selected: int
    processed: int
    paid: int
    failed: int
    skipped: int


class SchedulerRunErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool


class SchedulerRunMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    cron_job_name: str | None = Field(default=None, alias="cronJobName")


class SchedulerRunListItemResponse(BaseModel):
    run_id: str = Field(alias="runId")
    job_type: str = Field(alias="jobType")
    status: str
    trigger_source: str = Field(alias="triggerSource")
    worker_id: str = Field(alias="workerId")
    batch_size: int = Field(alias="batchSize")
    billing_date: date | None = Field(alias="billingDate")
    dry_run: bool = Field(alias="dryRun")
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime | None = Field(alias="finishedAt")
    duration_ms: int | None = Field(alias="durationMs")
    summary: SchedulerRunSummaryResponse
    operator_audit_id: str | None = Field(alias="operatorAuditId")


class SchedulerRunListResponse(BaseModel):
    items: list[SchedulerRunListItemResponse]
    page: SchedulerRunPageResponse


class SchedulerRunDetailResponse(SchedulerRunListItemResponse):
    error: SchedulerRunErrorResponse | None = None
    idempotency_key_id: str | None = Field(alias="idempotencyKeyId")
    request_id: str | None = Field(alias="requestId")
    exit_code: int | None = Field(alias="exitCode")
    metadata: SchedulerRunMetadataResponse


class AdminSchedulerRunRequest(BaseModel):
    job_type: object | None = Field(default=None, alias="jobType")
    billing_date: object | None = Field(default=None, alias="billingDate")
    limit: object = 100
    dry_run: object = Field(default=False, alias="dryRun")
    reason: object | None = None
    reason_message: object | None = Field(default=None, alias="reasonMessage")


class AdminSchedulerRunResponse(BaseModel):
    run_id: str = Field(alias="runId")
    job_type: str = Field(alias="jobType")
    status: str
    trigger_source: str = Field(alias="triggerSource")
    summary: SchedulerRunSummaryResponse
    operator_audit_id: str = Field(alias="operatorAuditId")


def scheduler_run_list_response(
    result: SchedulerRunListResult,
) -> SchedulerRunListResponse:
    return SchedulerRunListResponse(
        items=[_list_item_response(item) for item in result.items],
        page=SchedulerRunPageResponse(
            nextCursor=result.page.next_cursor,
            hasMore=result.page.has_more,
        ),
    )


def scheduler_run_detail_response(
    result: SchedulerRunDetailResult,
) -> SchedulerRunDetailResponse:
    return SchedulerRunDetailResponse(
        **_list_item_response(result).model_dump(by_alias=True),
        error=(
            SchedulerRunErrorResponse.model_validate(result.error)
            if result.error is not None
            else None
        ),
        idempotencyKeyId=result.idempotency_key_id,
        requestId=result.request_id,
        exitCode=result.exit_code,
        metadata=SchedulerRunMetadataResponse.model_validate(result.metadata),
    )


def admin_scheduler_run_response(
    result: AdminSchedulerRunResult,
) -> AdminSchedulerRunResponse:
    return AdminSchedulerRunResponse(
        runId=result.run_id,
        jobType=result.job_type,
        status=result.status,
        triggerSource=result.trigger_source,
        summary=_summary_response(result.summary),
        operatorAuditId=result.operator_audit_id,
    )


def _list_item_response(
    item: SchedulerRunListItem | SchedulerRunDetailResult,
) -> SchedulerRunListItemResponse:
    return SchedulerRunListItemResponse(
        runId=item.run_id,
        jobType=item.job_type,
        status=item.status,
        triggerSource=item.trigger_source,
        workerId=item.worker_id,
        batchSize=item.batch_size,
        billingDate=item.billing_date,
        dryRun=item.dry_run,
        startedAt=item.started_at,
        finishedAt=item.finished_at,
        durationMs=item.duration_ms,
        summary=_summary_response(item.summary),
        operatorAuditId=item.operator_audit_id,
    )


def _summary_response(summary: dict[str, int]) -> SchedulerRunSummaryResponse:
    return SchedulerRunSummaryResponse(
        selected=summary.get("selected", 0),
        processed=summary.get("processed", 0),
        paid=summary.get("paid", 0),
        failed=summary.get("failed", 0),
        skipped=summary.get("skipped", 0),
    )
