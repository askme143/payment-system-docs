from __future__ import annotations

from typing import Protocol

from payments.domain.entities.payment import Payment


class PaymentAttemptRepository(Protocol):
    async def save_payment(self, payment: Payment) -> None:
        raise NotImplementedError

    async def get_payment(self, payment_id: str) -> Payment | None:
        raise NotImplementedError

    async def get_payment_for_user(
        self,
        payment_id: str,
        user_id: str,
    ) -> Payment | None:
        raise NotImplementedError

    async def count_payments_for_checkout(self, checkout_id: str) -> int:
        raise NotImplementedError

    async def get_payment_attempt_no(self, checkout_id: str, payment_id: str) -> int:
        raise NotImplementedError

    async def count_user_payment_quantity_for_sku(
        self,
        user_id: str,
        sku_id: str,
        statuses: set[str],
    ) -> int:
        raise NotImplementedError
