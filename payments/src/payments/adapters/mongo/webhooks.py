from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.webhook_event import WebhookEvent


class MongoWebhookRepository:
    def __init__(
        self,
        *,
        webhook_events: AsyncIOMotorCollection,
        payments: AsyncIOMotorCollection,
        checkouts: AsyncIOMotorCollection,
        one_time_skus: AsyncIOMotorCollection,
        invoices: AsyncIOMotorCollection,
        subscriptions: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._webhook_events = webhook_events
        self._payments = payments
        self._checkouts = checkouts
        self._one_time_skus = one_time_skus
        self._invoices = invoices
        self._subscriptions = subscriptions
        self._session = session

    async def get_webhook_event(
        self,
        provider: str,
        event_id: str,
    ) -> WebhookEvent | None:
        document = await self._webhook_events.find_one(
            {"provider": provider, "event_id": event_id},
            session=self._session,
        )
        return _webhook_event_from_document(document)

    async def get_processed_webhook_event_by_payment_status(
        self,
        *,
        provider: str,
        payment_key: str,
        provider_status: str,
        exclude_event_id: str,
    ) -> WebhookEvent | None:
        cursor = self._webhook_events.find(
            {
                "provider": provider,
                "payload.paymentKey": payment_key,
                "payload.status": provider_status,
                "status": {"$in": ["processed", "ignored"]},
                "event_id": {"$ne": exclude_event_id},
            },
            session=self._session,
        )
        selected: WebhookEvent | None = None
        selected_timestamp: datetime | None = None
        async for document in cursor:
            event = _webhook_event_from_document(document)
            if event is None:
                continue
            event_timestamp = _provider_payload_timestamp(event.payload)
            if selected is None or _timestamp_is_later(
                event_timestamp,
                selected_timestamp,
            ):
                selected = event
                selected_timestamp = event_timestamp
        return selected

    async def save_webhook_event(self, event: WebhookEvent) -> None:
        document = to_document(event, omit_none=True)
        for transient_field in (
            "event_type",
            "payment_key",
            "order_id",
            "received_at",
            "processed_at",
        ):
            document.pop(transient_field, None)
        await self._webhook_events.replace_one(
            {"_id": event.id},
            document,
            upsert=True,
            session=self._session,
        )

    async def get_payment_by_order_or_key(
        self,
        *,
        order_id: str | None,
        payment_key: str | None,
    ) -> Payment | None:
        clauses: list[dict[str, str]] = []
        if order_id is not None:
            clauses.append({"order_id": order_id})
        if payment_key is not None:
            clauses.append({"payment_key": payment_key})
        if not clauses:
            return None
        document = await self._payments.find_one(
            {"$or": clauses},
            session=self._session,
        )
        return from_document(Payment, document)

    async def save_payment(self, payment: Payment) -> None:
        await self._payments.replace_one(
            {"_id": payment.id},
            to_document(payment, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def get_checkout(self, checkout_id: str) -> Checkout | None:
        return from_document(
            Checkout,
            await self._checkouts.find_one(
                {"_id": checkout_id},
                session=self._session,
            ),
        )

    async def mark_checkout_paid_if_ready(
        self,
        checkout_id: str,
        user_id: str,
        last_payment_id: str,
    ) -> bool:
        result = await self._checkouts.update_one(
            {"_id": checkout_id, "user_id": user_id, "status": "ready"},
            {"$set": {"status": "paid", "last_payment_id": last_payment_id}},
            session=self._session,
        )
        return result.modified_count == 1

    async def capture_checkout_reserved_stock(self, checkout: Checkout) -> None:
        for item in checkout.items:
            sku_id = item.get("skuId")
            quantity = item.get("quantity")
            if not isinstance(sku_id, str) or not isinstance(quantity, int):
                continue
            if quantity < 1:
                continue
            await self._one_time_skus.update_one(
                {
                    "_id": sku_id,
                    "stock_policy": "limited",
                    "reserved_stock": {"$gte": quantity},
                },
                {"$inc": {"reserved_stock": -quantity, "sold_stock": quantity}},
                session=self._session,
            )

    async def get_invoice_by_payment_id(self, payment_id: str) -> Invoice | None:
        return from_document(
            Invoice,
            await self._invoices.find_one(
                {"payment_id": payment_id},
                session=self._session,
            ),
        )

    async def save_invoice(self, invoice: Invoice) -> None:
        await self._invoices.replace_one(
            {"_id": invoice.id},
            to_document(invoice, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        return from_document(
            Subscription,
            await self._subscriptions.find_one(
                {"_id": subscription_id},
                session=self._session,
            ),
        )

    async def save_subscription(self, subscription: Subscription) -> None:
        await self._subscriptions.replace_one(
            {"_id": subscription.id},
            to_document(subscription, omit_none=True),
            upsert=True,
            session=self._session,
        )


def _webhook_event_from_document(
    document: dict[str, object] | None,
) -> WebhookEvent | None:
    event = from_document(WebhookEvent, document)
    if event is None:
        return None
    payload = event.payload
    event_type = payload.get("eventType")
    if isinstance(event_type, str):
        event.event_type = event_type
    payment_key = payload.get("paymentKey")
    if event.payment_key is None and isinstance(payment_key, str):
        event.payment_key = payment_key
    order_id = payload.get("orderId")
    if event.order_id is None and isinstance(order_id, str):
        event.order_id = order_id
    return event


def _provider_payload_timestamp(payload: dict[str, object]) -> datetime | None:
    for key in ("statusChangedAt", "canceledAt", "approvedAt"):
        value = payload.get(key)
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            continue
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            continue
    return None


def _timestamp_is_later(
    candidate: datetime | None,
    selected: datetime | None,
) -> bool:
    if selected is None:
        return candidate is not None
    return candidate is not None and candidate > selected
