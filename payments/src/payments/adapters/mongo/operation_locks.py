from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.ports.operation_locks import OperationLockRepository
from payments.domain.entities.operation_lock import OperationLock


class MongoOperationLockRepository(OperationLockRepository):
    def __init__(
        self,
        operation_locks: AsyncIOMotorCollection,
        operation_lock_counters: AsyncIOMotorCollection,
    ) -> None:
        self._operation_locks = operation_locks
        self._operation_lock_counters = operation_lock_counters

    async def acquire_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        fencing_counter_key: str,
        locked_until_at,
        acquired_at,
        metadata: dict[str, object] | None = None,
    ) -> OperationLock | None:
        counter = await self._operation_lock_counters.find_one_and_update(
            {"_id": fencing_counter_key},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        fencing_token = int(counter["seq"]) if counter is not None else 1
        operation_lock = OperationLock(
            id=OperationLock.generate_id(),
            lock_key=lock_key,
            owner_token=owner_token,
            fencing_token=fencing_token,
            fencing_counter_key=fencing_counter_key,
            status="active",
            locked_until_at=locked_until_at,
            acquired_at=acquired_at,
            metadata=metadata,
        )
        document = to_document(operation_lock, omit_none=True)
        try:
            acquired = await self._operation_locks.find_one_and_update(
                {
                    "lock_key": lock_key,
                    "$or": [
                        {"status": {"$ne": "active"}},
                        {"locked_until_at": {"$lte": acquired_at}},
                    ],
                },
                {
                    "$set": {
                        key: value for key, value in document.items() if key != "_id"
                    },
                    "$setOnInsert": {"_id": document["_id"]},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            return None
        return from_document(OperationLock, acquired)

    async def release_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        released_at,
    ) -> None:
        await self._operation_locks.update_one(
            {
                "lock_key": lock_key,
                "owner_token": owner_token,
                "status": "active",
            },
            {
                "$set": {
                    "status": "released",
                    "released_at": released_at,
                }
            },
        )
