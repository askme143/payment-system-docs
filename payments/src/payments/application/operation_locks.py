from __future__ import annotations

from datetime import datetime, timedelta

from payments.application.errors import InvalidStateTransitionError
from payments.application.ports.operation_locks import OperationLockRepository
from payments.domain.entities.ids import generate_uuid_id
from payments.domain.entities.operation_lock import OperationLock


async def acquire_required_operation_lock(
    *,
    operation_locks: OperationLockRepository | None,
    lock_key: str,
    fencing_counter_key: str,
    now: datetime,
    ttl: timedelta = timedelta(minutes=5),
    metadata: dict[str, object] | None = None,
) -> OperationLock | None:
    if operation_locks is None:
        return None
    operation_lock = await operation_locks.acquire_operation_lock(
        lock_key=lock_key,
        owner_token=generate_uuid_id("lock_owner"),
        fencing_counter_key=fencing_counter_key,
        locked_until_at=now + ttl,
        acquired_at=now,
        metadata=metadata,
    )
    if operation_lock is None:
        raise InvalidStateTransitionError("operation is locked")
    return operation_lock


async def release_operation_lock(
    *,
    operation_locks: OperationLockRepository | None,
    operation_lock: OperationLock | None,
    released_at: datetime,
) -> None:
    if operation_locks is None or operation_lock is None:
        return
    await operation_locks.release_operation_lock(
        lock_key=operation_lock.lock_key,
        owner_token=operation_lock.owner_token,
        released_at=released_at,
    )
