from __future__ import annotations

from datetime import datetime
from typing import Protocol

from payments.domain.entities.operation_lock import OperationLock


class OperationLockRepository(Protocol):
    async def acquire_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        fencing_counter_key: str,
        locked_until_at: datetime,
        acquired_at: datetime,
        metadata: dict[str, object] | None = None,
    ) -> OperationLock | None:
        raise NotImplementedError

    async def release_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        released_at: datetime,
    ) -> None:
        raise NotImplementedError
