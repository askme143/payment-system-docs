from __future__ import annotations

from typing import Protocol

from payments.domain.entities.checkout import Checkout
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.webhook_event import WebhookEvent


class WebhookRepository(Protocol):
    async def get_webhook_event(
        self,
        provider: str,
        event_id: str,
    ) -> WebhookEvent | None:
        raise NotImplementedError

    async def get_processed_webhook_event_by_payment_status(
        self,
        *,
        provider: str,
        payment_key: str,
        provider_status: str,
        exclude_event_id: str,
    ) -> WebhookEvent | None:
        raise NotImplementedError

    async def save_webhook_event(self, event: WebhookEvent) -> None:
        raise NotImplementedError

    async def get_payment_by_order_or_key(
        self,
        *,
        order_id: str | None,
        payment_key: str | None,
    ) -> Payment | None:
        raise NotImplementedError

    async def save_payment(self, payment: Payment) -> None:
        raise NotImplementedError

    async def get_checkout(self, checkout_id: str) -> Checkout | None:
        raise NotImplementedError

    async def mark_checkout_paid_if_ready(
        self,
        checkout_id: str,
        user_id: str,
        last_payment_id: str,
    ) -> bool:
        raise NotImplementedError

    async def capture_checkout_reserved_stock(self, checkout: Checkout) -> None:
        raise NotImplementedError

    async def get_invoice_by_payment_id(self, payment_id: str) -> Invoice | None:
        raise NotImplementedError

    async def save_invoice(self, invoice: Invoice) -> None:
        raise NotImplementedError

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        raise NotImplementedError

    async def save_subscription(self, subscription: Subscription) -> None:
        raise NotImplementedError
