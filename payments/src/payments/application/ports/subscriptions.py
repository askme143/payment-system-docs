from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription

SubscriptionStatus = Literal[
    "pending",
    "active",
    "past_due",
    "cancel_scheduled",
    "canceled",
]


@dataclass(frozen=True, slots=True)
class SubscriptionAccountRecord:
    subscription_id: str
    product_code: str
    plan_id: str
    plan_name: str
    status: SubscriptionStatus
    current_period_start_at: datetime | None
    current_period_end_at: datetime | None
    next_billing_at: datetime | None


@dataclass(frozen=True, slots=True)
class DefaultBillingMethodSummary:
    billing_method_id: str
    is_default: bool
    display_name: str


class SubscriptionAccountRepository(Protocol):
    async def list_user_subscription_records(
        self,
        user_id: str,
    ) -> list[SubscriptionAccountRecord]:
        raise NotImplementedError

    async def get_default_billing_method(
        self,
        user_id: str,
    ) -> DefaultBillingMethodSummary | None:
        raise NotImplementedError

    async def get_subscription_for_user(
        self,
        subscription_id: str,
        user_id: str,
    ) -> Subscription | None:
        raise NotImplementedError

    async def get_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        raise NotImplementedError

    async def schedule_subscription_cancel_at_period_end(
        self,
        subscription_id: str,
        user_id: str,
        canceled_at: datetime,
    ) -> Subscription:
        raise NotImplementedError

    async def resume_cancel_scheduled_subscription(
        self,
        subscription_id: str,
        user_id: str,
        resumed_at: datetime,
    ) -> Subscription:
        raise NotImplementedError

    async def save_subscription(self, subscription: Subscription) -> None:
        raise NotImplementedError


class SubscriptionCheckoutRepository(Protocol):
    async def count_active_subscriptions_for_user_product(
        self,
        user_id: str,
        product_code: str,
    ) -> int:
        raise NotImplementedError

    async def save_subscription(self, subscription: Subscription) -> None:
        raise NotImplementedError

    async def get_subscription_for_user(
        self,
        subscription_id: str,
        user_id: str,
    ) -> Subscription | None:
        raise NotImplementedError

    async def get_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        raise NotImplementedError

    async def save_payment(self, payment: Payment) -> None:
        raise NotImplementedError

    async def save_invoice(self, invoice: Invoice) -> None:
        raise NotImplementedError

    async def get_open_invoice_for_subscription_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Invoice | None:
        raise NotImplementedError


class SubscriptionExpirationRepository(Protocol):
    async def list_expired_cancel_scheduled_subscriptions(
        self,
        now: datetime,
        limit: int,
    ) -> list[Subscription]:
        raise NotImplementedError

    async def expire_cancel_scheduled_subscription(
        self,
        subscription_id: str,
        now: datetime,
    ) -> bool:
        raise NotImplementedError
