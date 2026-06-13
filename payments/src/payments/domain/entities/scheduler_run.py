from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id

SchedulerJobType = Literal["billing", "reminder", "cancel_expiration"]
SchedulerRunStatus = Literal["running", "succeeded", "failed", "canceled"]
SchedulerTriggerSource = Literal[
    "kubernetes_cronjob",
    "admin_manual",
    "internal_api",
    "cli",
]


@dataclass(slots=True)
class SchedulerRunLog:
    id: str
    job_type: SchedulerJobType
    status: SchedulerRunStatus
    trigger_source: SchedulerTriggerSource
    worker_id: str
    batch_size: int
    billing_date: date | None
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    summary: dict[str, int] = field(default_factory=dict)
    error: dict[str, object] | None = None
    idempotency_key_id: str | None = None
    operator_audit_id: str | None = None
    request_id: str | None = None
    exit_code: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("srun")
