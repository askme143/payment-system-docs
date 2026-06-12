from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection
from pymongo.errors import DuplicateKeyError

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.errors import InvalidStateTransitionError
from payments.application.ports.subscriptions import (
    DefaultBillingMethodSummary,
    SubscriptionAccountRecord,
    SubscriptionAccountRepository,
    SubscriptionCheckoutRepository,
    SubscriptionExpirationRepository,
)
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class MongoSubscriptionAccountRepository(SubscriptionAccountRepository):
    def __init__(
        self,
        subscriptions: AsyncIOMotorCollection,
        subscription_plans: AsyncIOMotorCollection,
        products: AsyncIOMotorCollection,
        billing_methods: AsyncIOMotorCollection,
        payment_instruments: AsyncIOMotorCollection | None = None,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._subscriptions = subscriptions
        self._subscription_plans = subscription_plans
        self._products = products
        self._billing_methods = billing_methods
        self._payment_instruments = payment_instruments
        self._session = session

    async def list_user_subscription_records(
        self,
        user_id: str,
    ) -> list[SubscriptionAccountRecord]:
        cursor = self._subscriptions.find(
            {"user_id": user_id},
            session=self._session,
        )
        rows: list[SubscriptionAccountRecord] = []
        async for subscription_document in cursor:
            subscription = from_document(Subscription, subscription_document)
            if subscription is None:
                continue
            plan = await self._get_plan(subscription.plan_id)
            if plan is None:
                continue
            product = await self._get_product(plan.product_id)
            if product is None:
                continue
            rows.append(
                SubscriptionAccountRecord(
                    subscription_id=subscription.id,
                    product_code=subscription.product_code,
                    plan_id=subscription.plan_id,
                    plan_name=_plan_display_name(product, plan),
                    status=subscription.status,
                    current_period_start_at=subscription.current_period_start_at,
                    current_period_end_at=subscription.current_period_end_at,
                    next_billing_at=subscription.next_billing_at,
                )
            )
        return rows

    async def get_default_billing_method(
        self,
        user_id: str,
    ) -> DefaultBillingMethodSummary | None:
        document = await self._billing_methods.find_one(
            {"user_id": user_id, "is_default": True, "status": "active"},
            session=self._session,
        )
        billing_method = from_document(BillingMethod, document)
        if billing_method is None:
            return None
        if self._payment_instruments is not None:
            instrument = await self._payment_instruments.find_one(
                {"_id": billing_method.instrument_id, "status": "active"},
                session=self._session,
            )
            if instrument is None:
                return None
        return DefaultBillingMethodSummary(
            billing_method_id=billing_method.id,
            is_default=billing_method.is_default,
            display_name=billing_method.display_name,
        )

    async def get_subscription_for_user(
        self,
        subscription_id: str,
        user_id: str,
    ) -> Subscription | None:
        return from_document(
            Subscription,
            await self._subscriptions.find_one(
                {"_id": subscription_id, "user_id": user_id},
                session=self._session,
            ),
        )

    async def get_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        return from_document(
            Subscription,
            await self._subscriptions.find_one(
                {"_id": subscription_id},
                session=self._session,
            ),
        )

    async def schedule_subscription_cancel_at_period_end(
        self,
        subscription_id: str,
        user_id: str,
        canceled_at: datetime,
    ) -> Subscription:
        subscription = await self.get_subscription_for_user(subscription_id, user_id)
        if subscription is None or subscription.current_period_end_at is None:
            raise LookupError("subscription was not found")
        result = await self._subscriptions.update_one(
            {"_id": subscription_id, "user_id": user_id, "status": "active"},
            {
                "$set": {
                    "status": "cancel_scheduled",
                    "cancel_at_period_end": True,
                    "cancel_at": subscription.current_period_end_at,
                    "access_until": subscription.current_period_end_at,
                    "updated_at": canceled_at,
                },
                "$unset": {"next_billing_at": ""},
            },
            session=self._session,
        )
        if result.modified_count != 1:
            raise LookupError("subscription was not cancelable")
        updated = await self.get_subscription_for_user(subscription_id, user_id)
        if updated is None:
            raise LookupError("subscription was not found")
        return updated

    async def resume_cancel_scheduled_subscription(
        self,
        subscription_id: str,
        user_id: str,
        resumed_at: datetime,
    ) -> Subscription:
        subscription = await self.get_subscription_for_user(subscription_id, user_id)
        if subscription is None:
            raise LookupError("subscription was not found")
        result = await self._subscriptions.update_one(
            {
                "_id": subscription_id,
                "user_id": user_id,
                "status": "cancel_scheduled",
                "current_period_end_at": {"$gt": resumed_at},
            },
            {
                "$set": {
                    "status": "active",
                    "cancel_at_period_end": False,
                    "next_billing_at": subscription.current_period_end_at,
                    "updated_at": resumed_at,
                },
                "$unset": {
                    "cancel_at": "",
                    "access_until": "",
                },
            },
            session=self._session,
        )
        if result.modified_count != 1:
            raise LookupError("subscription was not resumable")
        updated = await self.get_subscription_for_user(subscription_id, user_id)
        if updated is None:
            raise LookupError("subscription was not found")
        return updated

    async def save_subscription(self, subscription: Subscription) -> None:
        try:
            await self._subscriptions.replace_one(
                {"_id": subscription.id},
                to_document(subscription),
                upsert=True,
                session=self._session,
            )
        except DuplicateKeyError as exc:
            raise InvalidStateTransitionError(
                "active subscription already exists"
            ) from exc

    async def _get_plan(self, plan_id: str) -> SubscriptionPlan | None:
        return from_document(
            SubscriptionPlan,
            await self._subscription_plans.find_one(
                {"_id": plan_id},
                session=self._session,
            ),
        )

    async def _get_product(self, product_id: str) -> Product | None:
        return from_document(
            Product,
            await self._products.find_one(
                {"_id": product_id, "product_type": "subscription"},
                session=self._session,
            ),
        )


def _plan_display_name(product: Product, plan: SubscriptionPlan) -> str:
    period_label = "월간" if plan.billing_period == "monthly" else "연간"
    return f"{product.name} {period_label}"


class MongoSubscriptionCheckoutRepository(SubscriptionCheckoutRepository):
    def __init__(
        self,
        subscriptions: AsyncIOMotorCollection,
        payments: AsyncIOMotorCollection,
        invoices: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._subscriptions = subscriptions
        self._payments = payments
        self._invoices = invoices
        self._session = session

    async def count_active_subscriptions_for_user_product(
        self,
        user_id: str,
        product_code: str,
    ) -> int:
        return await self._subscriptions.count_documents(
            {
                "user_id": user_id,
                "product_code": product_code,
                "status": {
                    "$in": ["pending", "active", "past_due", "cancel_scheduled"]
                },
            },
            session=self._session,
        )

    async def save_subscription(self, subscription: Subscription) -> None:
        try:
            await self._subscriptions.replace_one(
                {"_id": subscription.id},
                to_document(subscription),
                upsert=True,
                session=self._session,
            )
        except DuplicateKeyError as exc:
            raise InvalidStateTransitionError(
                "active subscription already exists"
            ) from exc

    async def get_subscription_for_user(
        self,
        subscription_id: str,
        user_id: str,
    ) -> Subscription | None:
        document = await self._subscriptions.find_one(
            {"_id": subscription_id, "user_id": user_id},
            session=self._session,
        )
        return from_document(Subscription, document)

    async def get_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        document = await self._subscriptions.find_one(
            {"_id": subscription_id},
            session=self._session,
        )
        return from_document(Subscription, document)

    async def save_payment(self, payment: Payment) -> None:
        await self._payments.replace_one(
            {"_id": payment.id},
            to_document(payment, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def save_invoice(self, invoice: Invoice) -> None:
        await self._invoices.replace_one(
            {"_id": invoice.id},
            to_document(invoice, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def get_open_invoice_for_subscription_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Invoice | None:
        document = await self._invoices.find_one(
            {
                "subscription_id": subscription_id,
                "billing_cycle_key": billing_cycle_key,
                "status": {"$in": ["issued", "paid"]},
            },
            session=self._session,
        )
        return from_document(Invoice, document)


class MongoSubscriptionExpirationRepository(SubscriptionExpirationRepository):
    def __init__(
        self,
        subscriptions: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._subscriptions = subscriptions
        self._session = session

    async def list_expired_cancel_scheduled_subscriptions(
        self,
        now: datetime,
        limit: int,
    ) -> list[Subscription]:
        cursor = (
            self._subscriptions.find(
                {
                    "status": "cancel_scheduled",
                    "$or": [
                        {"current_period_end_at": {"$lte": now}},
                        {"cancel_at": {"$lte": now}},
                    ],
                },
                session=self._session,
            )
            .sort("current_period_end_at", 1)
            .limit(limit)
        )
        return [
            subscription
            for document in [document async for document in cursor]
            if (subscription := from_document(Subscription, document)) is not None
        ]

    async def expire_cancel_scheduled_subscription(
        self,
        subscription_id: str,
        now: datetime,
    ) -> bool:
        result = await self._subscriptions.update_one(
            {
                "_id": subscription_id,
                "status": "cancel_scheduled",
                "$or": [
                    {"current_period_end_at": {"$lte": now}},
                    {"cancel_at": {"$lte": now}},
                ],
            },
            [
                {
                    "$set": {
                        "status": "canceled",
                        "cancel_at_period_end": False,
                        "canceled_at": now,
                        "access_until": "$current_period_end_at",
                        "next_billing_at": None,
                    }
                }
            ],
            session=self._session,
        )
        return result.modified_count == 1
