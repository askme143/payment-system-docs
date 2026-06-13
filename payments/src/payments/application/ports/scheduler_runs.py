from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from payments.domain.entities.scheduler_run import (
    SchedulerJobType,
    SchedulerRunLog,
    SchedulerRunStatus,
    SchedulerTriggerSource,
)


@dataclass(frozen=True, slots=True)
class SchedulerRunQuery:
    job_type: tuple[SchedulerJobType, ...] | None = None
    status: tuple[SchedulerRunStatus, ...] | None = None
    trigger_source: tuple[SchedulerTriggerSource, ...] | None = None
    worker_id: str | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    cursor: str | None = None
    limit: int = 50


class SchedulerRunLogRepository(Protocol):
    async def list_scheduler_runs(
        self,
        query: SchedulerRunQuery,
    ) -> list[SchedulerRunLog]:
        raise NotImplementedError

    async def get_scheduler_run(self, run_id: str) -> SchedulerRunLog | None:
        raise NotImplementedError

    async def save_scheduler_run(self, run: SchedulerRunLog) -> None:
        raise NotImplementedError
