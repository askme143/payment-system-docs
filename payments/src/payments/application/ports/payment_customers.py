from __future__ import annotations

from typing import Protocol

from payments.domain.entities.payment_customer import PaymentCustomer


class PaymentCustomerRepository(Protocol):
    async def get_active_payment_customer_for_user(
        self,
        user_id: str,
    ) -> PaymentCustomer | None:
        raise NotImplementedError

    async def save_payment_customer(self, payment_customer: PaymentCustomer) -> None:
        raise NotImplementedError
