from __future__ import annotations

from datetime import datetime
from typing import Protocol

from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class BillingRetryRepository(Protocol):
    async def list_due_active_subscriptions(
        self,
        billing_cutoff_at: datetime,
        limit: int,
    ) -> list[Subscription]:
        raise NotImplementedError

    async def list_reminder_subscriptions(
        self,
        reminder_start_at: datetime,
        reminder_end_at: datetime,
        limit: int,
    ) -> list[Subscription]:
        raise NotImplementedError

    async def count_excluded_billing_subscriptions(self) -> int:
        raise NotImplementedError

    async def get_subscription_plan(
        self,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        raise NotImplementedError

    async def get_invoice_by_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Invoice | None:
        raise NotImplementedError

    async def get_invoice(self, invoice_id: str) -> Invoice | None:
        raise NotImplementedError

    async def get_payment(self, payment_id: str) -> Payment | None:
        raise NotImplementedError

    async def get_latest_failed_payment_for_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Payment | None:
        raise NotImplementedError

    async def count_failed_payments_for_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> int:
        raise NotImplementedError

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        raise NotImplementedError

    async def get_default_billing_method(self, user_id: str) -> BillingMethod | None:
        raise NotImplementedError

    async def get_payment_instrument(
        self,
        instrument_id: str,
    ) -> PaymentInstrument | None:
        raise NotImplementedError

    async def save_payment(self, payment: Payment) -> None:
        raise NotImplementedError

    async def save_invoice(self, invoice: Invoice) -> None:
        raise NotImplementedError

    async def save_subscription(self, subscription: Subscription) -> None:
        raise NotImplementedError

    async def save_subscription_billing_result(
        self,
        *,
        payment: Payment,
        invoice: Invoice,
        subscription: Subscription,
        expected_next_billing_at: datetime,
    ) -> bool:
        raise NotImplementedError
