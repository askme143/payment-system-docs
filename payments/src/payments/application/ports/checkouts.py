from __future__ import annotations

from typing import Protocol

from payments.domain.entities.checkout import Checkout


class CheckoutRepository(Protocol):
    async def save_checkout(self, checkout: Checkout) -> None:
        raise NotImplementedError

    async def get_checkout_for_user(
        self,
        checkout_id: str,
        user_id: str,
    ) -> Checkout | None:
        raise NotImplementedError
