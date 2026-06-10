from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.domain.entities.idempotency_key import IdempotencyKey


class MongoIdempotencyKeyRepository(IdempotencyKeyRepository):
    def __init__(
        self,
        idempotency_keys: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._idempotency_keys = idempotency_keys
        self._session = session

    async def find_idempotency_key(
        self,
        scope: str,
        key_hash: str,
    ) -> IdempotencyKey | None:
        document = await self._idempotency_keys.find_one(
            {"scope": scope, "key_hash": key_hash},
            session=self._session,
        )
        return from_document(IdempotencyKey, document)

    async def save_idempotency_key(self, key: IdempotencyKey) -> None:
        await self._idempotency_keys.replace_one(
            {"_id": key.id},
            to_document(key),
            upsert=True,
            session=self._session,
        )
