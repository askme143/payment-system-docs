from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from payments.application.admin_catalog import AdminRequestContext
from payments.application.scheduler_runs import (
    AdminSchedulerRunCommand,
    SchedulerRunQuery,
    list_scheduler_runs,
    run_admin_scheduler_run,
)
from payments.domain.entities.scheduler_run import SchedulerRunLog


class FixedClock:
    def utc_now(self) -> datetime:
        return datetime(2026, 6, 10, 0, 0, tzinfo=UTC)


class FakeSchedulerRunRepository:
    def __init__(self) -> None:
        self.runs: dict[str, SchedulerRunLog] = {}
        self.queries: list[SchedulerRunQuery] = []

    async def list_scheduler_runs(
        self,
        query: SchedulerRunQuery,
    ) -> list[SchedulerRunLog]:
        self.queries.append(query)
        runs = list(self.runs.values())
        if query.job_type is not None:
            runs = [run for run in runs if run.job_type in query.job_type]
        runs = sorted(runs, key=lambda run: (run.started_at, run.id), reverse=True)
        return runs[: query.limit]

    async def get_scheduler_run(self, run_id: str) -> SchedulerRunLog | None:
        return self.runs.get(run_id)

    async def save_scheduler_run(self, run: SchedulerRunLog) -> None:
        self.runs[run.id] = run


async def _fake_billing_job(
    *,
    job_type: str,
    billing_date: date | None,
    limit: int,
    dry_run: bool,
):
    assert job_type == "billing"
    assert billing_date == date(2026, 6, 10)
    assert limit == 12
    assert dry_run is False
    return {
        "billingDate": "2026-06-10",
        "processed": 12,
        "paid": 11,
        "failed": 1,
        "skipped": 0,
        "excludedCancelScheduled": 0,
        "reminderEmailsSent": 0,
    }


async def test_list_scheduler_runs_returns_page_and_stable_cursor() -> None:
    repository = FakeSchedulerRunRepository()
    repository.runs["srun_old"] = SchedulerRunLog(
        id="srun_old",
        job_type="billing",
        status="succeeded",
        trigger_source="kubernetes_cronjob",
        worker_id="pod_old",
        batch_size=100,
        billing_date=date(2026, 6, 9),
        dry_run=False,
        started_at=datetime(2026, 6, 9, tzinfo=UTC),
        finished_at=datetime(2026, 6, 9, 0, 0, 5, tzinfo=UTC),
        duration_ms=5000,
        summary={"processed": 5, "failed": 0},
    )
    repository.runs["srun_new"] = SchedulerRunLog(
        id="srun_new",
        job_type="billing",
        status="succeeded",
        trigger_source="admin_manual",
        worker_id="admin_1:req_1",
        batch_size=100,
        billing_date=date(2026, 6, 10),
        dry_run=False,
        started_at=datetime(2026, 6, 10, tzinfo=UTC),
        finished_at=datetime(2026, 6, 10, 0, 0, 5, tzinfo=UTC),
        duration_ms=5000,
        summary={"processed": 10, "failed": 1},
        operator_audit_id="oaudit_1",
    )

    result = await list_scheduler_runs(
        SchedulerRunQuery(job_type=("billing",), limit=1),
        repository,
    )

    assert [item.run_id for item in result.items] == ["srun_new"]
    assert result.page.has_more is True
    assert result.page.next_cursor is not None
    assert result.items[0].operator_audit_id == "oaudit_1"


async def test_run_admin_scheduler_run_saves_success_log_and_audit() -> None:
    repository = FakeSchedulerRunRepository()
    audits: list[dict[str, object]] = []

    result = await run_admin_scheduler_run(
        context=AdminRequestContext(
            request_id="req_admin_scheduler",
            admin_id="admin_1",
            request_ip="203.0.113.10",
        ),
        command=AdminSchedulerRunCommand(
            job_type="billing",
            billing_date=date(2026, 6, 10),
            limit=12,
            dry_run=False,
            reason_code="manual_retry_after_cron_failure",
        ),
        scheduler_runs=repository,
        clock=FixedClock(),
        idempotency_key="idem_admin_scheduler",
        idempotency_keys=None,
        billing_job=_fake_billing_job,
        expiration_job=None,
        save_operator_audit=lambda audit: audits.append(
            {
                "action": audit.action,
                "target_type": audit.target_type,
                "target_id": audit.target_id,
                "result": audit.result,
                "next_state": audit.next_state,
            }
        ),
    )

    saved = repository.runs[result.run_id]
    assert result.status == "succeeded"
    assert result.summary["failed"] == 1
    assert saved.status == "succeeded"
    assert saved.trigger_source == "admin_manual"
    assert saved.worker_id == "admin_1:req_admin_scheduler"
    assert saved.exit_code == 0
    assert audits == [
        {
            "action": "scheduler.run_manual",
            "target_type": "scheduler_run",
            "target_id": result.run_id,
            "result": "succeeded",
            "next_state": {
                "run_id": result.run_id,
                "job_type": "billing",
                "status": "succeeded",
                "summary": result.summary,
            },
        }
    ]


async def test_run_admin_scheduler_run_marks_run_failed_for_run_level_error() -> None:
    repository = FakeSchedulerRunRepository()
    audits: list[dict[str, object]] = []

    async def failing_job(**_: object) -> dict[str, object]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await run_admin_scheduler_run(
            context=AdminRequestContext(
                request_id="req_admin_scheduler",
                admin_id="admin_1",
            ),
            command=AdminSchedulerRunCommand(
                job_type="billing",
                billing_date=date(2026, 6, 10),
                limit=12,
                dry_run=False,
                reason_code="manual_retry_after_cron_failure",
            ),
            scheduler_runs=repository,
            clock=FixedClock(),
            idempotency_key="idem_admin_scheduler",
            idempotency_keys=None,
            billing_job=failing_job,
            expiration_job=None,
            save_operator_audit=lambda audit: audits.append(
                {
                    "action": audit.action,
                    "target_id": audit.target_id,
                    "result": audit.result,
                    "next_state": audit.next_state,
                }
            ),
        )

    saved = next(iter(repository.runs.values()))
    assert saved.status == "failed"
    assert saved.exit_code == 1
    assert saved.operator_audit_id is not None
    assert saved.error == {
        "code": "SCHEDULER_RUN_FAILED",
        "message": "scheduler run failed",
        "retryable": True,
    }
    assert audits == [
        {
            "action": "scheduler.run_manual",
            "target_id": saved.id,
            "result": "failed",
            "next_state": {
                "run_id": saved.id,
                "job_type": "billing",
                "status": "failed",
                "error": saved.error,
            },
        }
    ]
