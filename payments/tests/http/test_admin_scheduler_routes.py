from __future__ import annotations

from datetime import UTC, date, datetime

from payments.domain.entities.scheduler_run import SchedulerRunLog


def test_list_admin_scheduler_runs_returns_documented_shape(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.scheduler_runs.runs["srun_1"] = SchedulerRunLog(
        id="srun_1",
        job_type="billing",
        status="succeeded",
        trigger_source="kubernetes_cronjob",
        worker_id="payments-billing-1",
        batch_size=100,
        billing_date=date(2026, 6, 10),
        dry_run=False,
        started_at=datetime(2026, 6, 10, tzinfo=UTC),
        finished_at=datetime(2026, 6, 10, 0, 0, 1, tzinfo=UTC),
        duration_ms=1000,
        summary={"selected": 0, "processed": 0, "paid": 0, "failed": 0, "skipped": 0},
    )

    response = client.get(
        "/admin/scheduler-runs?jobType=billing",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "runId": "srun_1",
                "jobType": "billing",
                "status": "succeeded",
                "triggerSource": "kubernetes_cronjob",
                "workerId": "payments-billing-1",
                "batchSize": 100,
                "billingDate": "2026-06-10",
                "dryRun": False,
                "startedAt": "2026-06-10T00:00:00Z",
                "finishedAt": "2026-06-10T00:00:01Z",
                "durationMs": 1000,
                "summary": {
                    "selected": 0,
                    "processed": 0,
                    "paid": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                "operatorAuditId": None,
            }
        ],
        "page": {"nextCursor": None, "hasMore": False},
    }


def test_get_admin_scheduler_run_detail_returns_error_summary(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.scheduler_runs.runs["srun_failed"] = SchedulerRunLog(
        id="srun_failed",
        job_type="billing",
        status="failed",
        trigger_source="admin_manual",
        worker_id="admin_1:req_admin",
        batch_size=12,
        billing_date=date(2026, 6, 10),
        dry_run=False,
        started_at=datetime(2026, 6, 10, tzinfo=UTC),
        finished_at=datetime(2026, 6, 10, 0, 0, 1, tzinfo=UTC),
        duration_ms=1000,
        summary={"selected": 1, "processed": 0, "paid": 0, "failed": 0, "skipped": 0},
        error={
            "code": "SCHEDULER_RUN_FAILED",
            "message": "scheduler run failed",
            "retryable": True,
        },
        operator_audit_id="oaudit_1",
        request_id="req_admin",
        exit_code=1,
    )

    response = client.get("/admin/scheduler-runs/srun_failed", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["runId"] == "srun_failed"
    assert body["status"] == "failed"
    assert body["error"] == {
        "code": "SCHEDULER_RUN_FAILED",
        "message": "scheduler run failed",
        "retryable": True,
    }
    assert body["operatorAuditId"] == "oaudit_1"
    assert body["exitCode"] == 1


def test_create_admin_scheduler_run_executes_synchronously_and_records_audit(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    response = client.post(
        "/admin/scheduler-runs",
        headers={**admin_headers, "Idempotency-Key": "idem_scheduler_manual"},
        json={
            "jobType": "billing",
            "billingDate": "2026-06-10",
            "limit": 12,
            "dryRun": False,
            "reason": "manual_retry_after_cron_failure",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["runId"].startswith("srun_")
    assert body["status"] == "succeeded"
    assert body["triggerSource"] == "admin_manual"
    assert body["summary"]["failed"] == 0
    saved = test_dependencies.scheduler_runs.runs[body["runId"]]
    assert saved.finished_at is not None
    assert saved.operator_audit_id == body["operatorAuditId"]
    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        body["operatorAuditId"]
    ]
    assert audit.action == "scheduler.run_manual"
    assert audit.target_id == body["runId"]
