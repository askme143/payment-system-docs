from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from payments.domain.entities.invoice import Invoice

InvoiceStatus = Literal["issued", "paid", "voided", "refunded"]
PaymentStatus = Literal[
    "ready", "paid", "failed", "expired", "canceled", "partial_canceled"
]
SubscriptionStatus = Literal[
    "pending", "active", "past_due", "cancel_scheduled", "canceled"
]


@dataclass(frozen=True, slots=True)
class InvoiceListRecord:
    invoice_id: str
    subscription_id: str | None
    product_name: str
    plan_name: str
    invoice_type: str
    status: InvoiceStatus
    payment_status: PaymentStatus | None
    amount: int
    currency: str
    billing_date: date
    paid_at: datetime | None
    receipt_available: bool
    failure_summary: str | None


@dataclass(frozen=True, slots=True)
class InvoiceDetailRecord:
    invoice_id: str
    subscription_id: str | None
    status: InvoiceStatus
    payment_status: PaymentStatus | None
    amount: int
    currency: str
    billing_date: date
    paid_at: datetime | None
    receipt_url: str | None
    failure_code: str | None
    failure_reason: str | None
    failure_message: str | None
    failure_retryable: bool
    retry_available: bool
    retry_scheduled_at: datetime | None
    subscription_status: SubscriptionStatus | None = None


class InvoiceRepository(Protocol):
    async def list_invoices_for_user(
        self,
        user_id: str,
        limit: int,
        status: InvoiceStatus | None = None,
        payment_status: PaymentStatus | None = None,
        subscription_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        cursor: str | None = None,
    ) -> list[InvoiceListRecord]:
        raise NotImplementedError

    async def get_invoice_detail_for_user(
        self,
        invoice_id: str,
        user_id: str,
    ) -> InvoiceDetailRecord | None:
        raise NotImplementedError

    async def get_invoice_owner(self, invoice_id: str) -> str | None:
        raise NotImplementedError


class InvoiceWriteRepository(Protocol):
    async def save_invoice(self, invoice: Invoice) -> None:
        raise NotImplementedError
