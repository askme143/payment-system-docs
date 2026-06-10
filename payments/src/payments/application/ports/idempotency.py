from __future__ import annotations

from typing import Protocol

from payments.domain.entities.idempotency_key import IdempotencyKey


class IdempotencyKeyRepository(Protocol):
    async def find_idempotency_key(
        self,
        scope: str,
        key_hash: str,
    ) -> IdempotencyKey | None:
        raise NotImplementedError

    async def save_idempotency_key(self, key: IdempotencyKey) -> None:
        raise NotImplementedError
