from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


@dataclass(frozen=True, slots=True)
class AdminListQuery:
    status: str | tuple[str, ...] | None = None
    user_id: str | None = None
    order_id: str | None = None
    payment_key: str | None = None
    product_code: str | None = None
    payment_failure: bool | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    next_billing_from: datetime | None = None
    next_billing_to: datetime | None = None
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class AdminPaymentListRecord:
    payment_id: str
    checkout_id: str | None
    user_id: str | None
    user_email: str | None
    order_id: str
    order_name: str
    payment_key: str | None
    status: str
    amount: int
    paid_amount: int
    cancelable_amount: int
    currency: str
    created_at: datetime
    approved_at: datetime | None
    method_summary: str | None


@dataclass(frozen=True, slots=True)
class AdminSubscriptionListRecord:
    subscription_id: str
    user_id: str
    user_email: str | None
    product_code: str
    product_name: str
    plan_id: str
    plan_name: str
    status: str
    current_period_start_at: datetime | None
    current_period_end_at: datetime | None
    next_billing_at: datetime | None
    payment_failure: dict[str, object] | None
    default_billing_method_summary: str | None


class AdminOperationsRepository(Protocol):
    async def list_admin_payments(
        self,
        query: AdminListQuery,
    ) -> list[AdminPaymentListRecord]:
        raise NotImplementedError

    async def list_admin_subscriptions(
        self,
        query: AdminListQuery,
    ) -> list[AdminSubscriptionListRecord]:
        raise NotImplementedError

    async def save_admin_list_audit_record(
        self,
        *,
        audit_id: str,
        admin_id: str,
        request_id: str,
        action: str,
        target_type: str,
        target_id: str,
        query: dict[str, object],
        result_count: int,
        has_more: bool,
        request_ip: str | None,
        created_at: datetime,
    ) -> None:
        raise NotImplementedError

    async def get_admin_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        raise NotImplementedError

    async def get_admin_subscription_plan(
        self,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        raise NotImplementedError

    async def get_admin_payment_by_invoice_id(
        self,
        invoice_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        raise NotImplementedError

    async def get_admin_payment_by_payment_key(
        self,
        payment_key: str,
    ) -> Payment | None:
        raise NotImplementedError

    async def get_admin_invoice_by_payment_id(
        self,
        payment_id: str,
    ) -> Invoice | None:
        raise NotImplementedError

    async def get_admin_latest_failed_subscription_payment(
        self,
        subscription_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        raise NotImplementedError

    async def save_admin_subscription(self, subscription: Subscription) -> None:
        raise NotImplementedError

    async def save_admin_payment(self, payment: Payment) -> None:
        raise NotImplementedError

    async def save_admin_invoice(self, invoice: Invoice) -> None:
        raise NotImplementedError

    async def save_subscription_adjustment_audit_record(
        self,
        *,
        audit_id: str,
        subscription_id: str,
        admin_id: str,
        request_id: str,
        adjustment_type: str,
        reason_code: str,
        reason_message: str,
        previous: dict[str, object],
        next_value: dict[str, object],
        notified_customer: bool,
        request_ip: str | None = None,
        result: str = "succeeded",
        idempotency_key_id: str | None = None,
        idempotency_scope: str | None = None,
        idempotency_key_hash: str | None = None,
        idempotency_request_hash: str | None = None,
    ) -> None:
        raise NotImplementedError
