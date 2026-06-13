from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Query, status

from payments.application.admin_catalog import AdminRequestContext
from payments.application.errors import BadRequestError
from payments.application.jobs.subscription_billing import (
    SubscriptionBillingRunCommand,
    run_subscription_billing,
)
from payments.application.jobs.subscription_expiration import (
    expire_cancel_scheduled_subscriptions,
)
from payments.application.ports.scheduler_runs import SchedulerRunQuery
from payments.application.scheduler_runs import (
    AdminSchedulerRunCommand,
    get_scheduler_run_detail,
    list_scheduler_runs,
    run_admin_scheduler_run,
)
from payments.domain.entities.scheduler_run import (
    SchedulerJobType,
    SchedulerRunStatus,
    SchedulerTriggerSource,
)
from payments.http.dependencies import HttpDependencies, admin_context_dependency
from payments.http.schemas.scheduler_runs import (
    AdminSchedulerRunRequest,
    AdminSchedulerRunResponse,
    SchedulerRunDetailResponse,
    SchedulerRunListResponse,
    admin_scheduler_run_response,
    scheduler_run_detail_response,
    scheduler_run_list_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin-scheduler-runs"])
    require_scheduler_read_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("scheduler_read", "scheduler_run"),
    )
    require_scheduler_run_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("scheduler_run",),
    )

    @router.get("/scheduler-runs", response_model=SchedulerRunListResponse)
    async def list_runs(
        context: AdminRequestContext = Depends(require_scheduler_read_context),
        jobType: Annotated[list[str] | None, Query()] = None,
        status: Annotated[list[str] | None, Query()] = None,
        triggerSource: Annotated[list[str] | None, Query()] = None,
        workerId: str | None = None,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        cursor: str | None = None,
        limit: str = "50",
    ) -> SchedulerRunListResponse:
        _ = context
        result = await list_scheduler_runs(
            SchedulerRunQuery(
                job_type=_job_types(jobType),
                status=_run_statuses(status),
                trigger_source=_trigger_sources(triggerSource),
                worker_id=workerId,
                from_at=_query_datetime(from_, "from"),
                to_at=_query_datetime(to, "to"),
                cursor=cursor,
                limit=_query_limit(limit),
            ),
            dependencies.scheduler_runs,
        )
        return scheduler_run_list_response(result)

    @router.get(
        "/scheduler-runs/{runId}",
        response_model=SchedulerRunDetailResponse,
    )
    async def get_run_detail(
        runId: str,
        context: AdminRequestContext = Depends(require_scheduler_read_context),
    ) -> SchedulerRunDetailResponse:
        _ = context
        result = await get_scheduler_run_detail(runId, dependencies.scheduler_runs)
        return scheduler_run_detail_response(result)

    @router.post(
        "/scheduler-runs",
        response_model=AdminSchedulerRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_run(
        request: AdminSchedulerRunRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        context: AdminRequestContext = Depends(require_scheduler_run_context),
    ) -> AdminSchedulerRunResponse:
        result = await run_admin_scheduler_run(
            context=context,
            command=AdminSchedulerRunCommand(
                job_type=_job_type(_required_text(request.job_type, "jobType")),
                billing_date=_body_date(request.billing_date, "billingDate"),
                limit=_body_limit(request.limit),
                dry_run=_body_bool(request.dry_run, "dryRun"),
                reason_code=_required_text(request.reason, "reason"),
                reason_message=_optional_text(request.reason_message, "reasonMessage"),
            ),
            scheduler_runs=dependencies.scheduler_runs,
            clock=dependencies.clock,
            idempotency_key=_required_header(idempotency_key, "Idempotency-Key"),
            idempotency_keys=dependencies.idempotency_keys,
            billing_job=_billing_job(dependencies),
            expiration_job=_expiration_job(dependencies),
            save_operator_audit=dependencies.operator_audits.save_operator_audit,
        )
        return admin_scheduler_run_response(result)

    return router


def _billing_job(dependencies: HttpDependencies):
    async def run_job(
        *,
        job_type: str,
        billing_date: date | None,
        limit: int,
        dry_run: bool,
    ) -> object:
        return await run_subscription_billing(
            SubscriptionBillingRunCommand(
                job_type="reminder" if job_type == "reminder" else "billing",
                billing_date=billing_date,
                limit=limit,
                dry_run=dry_run,
            ),
            dependencies.billing_retries,
            dependencies.payment_customers,
            dependencies.idempotency_keys,
            dependencies.payment_provider,
            dependencies.clock,
            dependencies.billing_key_cipher,
            idempotency_key=None,
            operation_locks=dependencies.operation_locks,
            subscription_billing_uow_factory=(
                dependencies.subscription_billing_uow_factory
            ),
            notification_dependencies=dependencies.notification_enqueue,
        )

    return run_job


def _expiration_job(dependencies: HttpDependencies):
    async def run_job(*, limit: int, dry_run: bool) -> object:
        return await expire_cancel_scheduled_subscriptions(
            subscriptions=dependencies.subscription_expirations,
            clock=dependencies.clock,
            limit=limit,
            dry_run=dry_run,
            operation_locks=dependencies.operation_locks,
            subscription_expiration_uow_factory=(
                dependencies.subscription_expiration_uow_factory
            ),
            operator_audits=dependencies.operator_audits,
            notification_dependencies=dependencies.notification_enqueue,
        )

    return run_job


def _query_datetime(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequestError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BadRequestError(f"{field_name} is invalid")
    return parsed


def _query_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise BadRequestError("limit is invalid") from exc
    if limit < 1 or limit > 100:
        raise BadRequestError("limit is invalid")
    return limit


def _body_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequestError("limit is invalid")
    if value < 1 or value > 100:
        raise BadRequestError("limit is invalid")
    return value


def _body_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise BadRequestError(f"{field_name} is invalid")
    return value


def _required_header(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value


def _required_text(value: object | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value.strip()


def _optional_text(value: object | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is invalid")
    return value.strip()


def _body_date(value: object | None, field_name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise BadRequestError(f"{field_name} is invalid") from exc
    raise BadRequestError(f"{field_name} is invalid")


def _job_types(values: list[str] | None) -> tuple[SchedulerJobType, ...] | None:
    if values is None:
        return None
    return tuple(_job_type(value) for value in values)


def _job_type(value: str) -> SchedulerJobType:
    if value in {"billing", "reminder", "cancel_expiration"}:
        return cast(SchedulerJobType, value)
    raise BadRequestError("jobType is invalid")


def _run_statuses(values: list[str] | None) -> tuple[SchedulerRunStatus, ...] | None:
    if values is None:
        return None
    valid = {"running", "succeeded", "failed", "canceled"}
    if set(values) - valid:
        raise BadRequestError("status is invalid")
    return tuple(cast(SchedulerRunStatus, value) for value in values)


def _trigger_sources(
    values: list[str] | None,
) -> tuple[SchedulerTriggerSource, ...] | None:
    if values is None:
        return None
    valid = {"kubernetes_cronjob", "admin_manual", "internal_api", "cli"}
    if set(values) - valid:
        raise BadRequestError("triggerSource is invalid")
    return tuple(cast(SchedulerTriggerSource, value) for value in values)
