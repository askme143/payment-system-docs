from __future__ import annotations

from typing import Protocol

from payments.domain.entities.billing_auth import BillingAuth
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.payment_instrument import PaymentInstrument


class BillingAuthRepository(Protocol):
    async def get_customer_key_for_user(self, user_id: str) -> str | None:
        raise NotImplementedError

    async def save_customer_key_for_user(
        self,
        user_id: str,
        customer_key: str,
    ) -> None:
        raise NotImplementedError

    async def count_active_billing_methods_for_user(self, user_id: str) -> int:
        raise NotImplementedError

    async def save_billing_auth(self, billing_auth: BillingAuth) -> None:
        raise NotImplementedError

    async def get_billing_auth_for_user(
        self,
        billing_auth_id: str,
        user_id: str,
    ) -> BillingAuth | None:
        raise NotImplementedError

    async def clear_default_billing_methods_for_user(self, user_id: str) -> None:
        raise NotImplementedError

    async def save_payment_instrument(
        self,
        instrument: PaymentInstrument,
    ) -> None:
        raise NotImplementedError

    async def save_billing_method(self, billing_method: BillingMethod) -> None:
        raise NotImplementedError
