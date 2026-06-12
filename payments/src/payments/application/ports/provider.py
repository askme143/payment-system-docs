from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PaymentConfirmProviderResult:
    payment_key: str
    order_id: str
    amount: int
    approved_at: datetime
    receipt_url: str | None
    method: str
    method_detail: dict[str, object]
    response_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class PaymentCancelProviderResult:
    cancel_id: str
    cancel_amount: int
    canceled_amount: int
    cancelable_amount: int
    canceled_at: datetime
    receipt_url: str | None


@dataclass(frozen=True, slots=True)
class PaymentLookupProviderResult:
    payment_key: str
    order_id: str
    status: str
    total_amount: int
    approved_at: datetime | None
    receipt_url: str | None
    method: str
    method_detail: dict[str, object]
    response_summary: dict[str, object]
    canceled_amount: int = 0
    cancelable_amount: int | None = None


@dataclass(frozen=True, slots=True)
class BillingKeyIssueProviderResult:
    billing_key: str
    method: str
    card_company: str
    masked_card_number: str
    response_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class BillingChargeProviderResult:
    payment_key: str
    order_id: str
    amount: int
    approved_at: datetime
    receipt_url: str | None
    method: str
    method_detail: dict[str, object]
    response_summary: dict[str, object]


class PaymentProvider(Protocol):
    async def confirm_payment(
        self,
        *,
        payment_key: str,
        order_id: str,
        amount: int,
        idempotency_key: str | None = None,
    ) -> PaymentConfirmProviderResult:
        raise NotImplementedError

    async def cancel_payment(
        self,
        *,
        payment_key: str,
        cancel_amount: int,
        cancel_reason: str,
        refund_bank_account: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentCancelProviderResult:
        raise NotImplementedError

    async def get_payment(
        self,
        *,
        payment_key: str,
    ) -> PaymentLookupProviderResult:
        raise NotImplementedError

    async def issue_billing_key(
        self,
        *,
        auth_key: str,
        customer_key: str,
    ) -> BillingKeyIssueProviderResult:
        raise NotImplementedError

    async def charge_billing_key(
        self,
        *,
        billing_key: str,
        customer_key: str,
        order_id: str,
        amount: int,
        order_name: str,
        idempotency_key: str | None = None,
    ) -> BillingChargeProviderResult:
        raise NotImplementedError
