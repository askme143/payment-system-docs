from __future__ import annotations

from typing import Protocol

from payments.domain.entities.payment import Payment


class PaymentAttemptRepository(Protocol):
    async def save_payment(self, payment: Payment) -> None:
        raise NotImplementedError

    async def get_payment_for_user(
        self,
        payment_id: str,
        user_id: str,
    ) -> Payment | None:
        raise NotImplementedError
