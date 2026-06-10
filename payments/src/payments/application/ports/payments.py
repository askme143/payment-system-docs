from __future__ import annotations

from typing import Protocol

from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.payment import Payment


class PaymentRepository(Protocol):
    async def find_idempotency_key(
        self,
        scope: str,
        key_hash: str,
    ) -> IdempotencyKey | None:
        raise NotImplementedError

    async def save_idempotency_key(self, key: IdempotencyKey) -> None:
        raise NotImplementedError

    async def save_checkout(self, checkout: Checkout) -> None:
        raise NotImplementedError

    async def save_payment(self, payment: Payment) -> None:
        raise NotImplementedError

    async def get_checkout_for_user(
        self, checkout_id: str, user_id: str
    ) -> Checkout | None:
        raise NotImplementedError

    async def get_payment_for_user(
        self, payment_id: str, user_id: str
    ) -> Payment | None:
        raise NotImplementedError
