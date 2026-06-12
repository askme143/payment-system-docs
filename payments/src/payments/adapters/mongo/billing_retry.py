from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class MongoBillingRetryRepository:
    def __init__(
        self,
        *,
        invoices: AsyncIOMotorCollection,
        payments: AsyncIOMotorCollection,
        subscriptions: AsyncIOMotorCollection,
        subscription_plans: AsyncIOMotorCollection,
        billing_methods: AsyncIOMotorCollection,
        payment_instruments: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._invoices = invoices
        self._payments = payments
        self._subscriptions = subscriptions
        self._subscription_plans = subscription_plans
        self._billing_methods = billing_methods
        self._payment_instruments = payment_instruments
        self._session = session

    async def list_due_active_subscriptions(
        self,
        billing_cutoff_at,
        limit: int,
    ) -> list[Subscription]:
        cursor = (
            self._subscriptions.find(
                {
                    "status": "active",
                    "next_billing_at": {"$ne": None, "$lte": billing_cutoff_at},
                },
                session=self._session,
            )
            .sort("next_billing_at", 1)
            .limit(limit)
        )
        subscriptions: list[Subscription] = []
        async for document in cursor:
            subscription = from_document(Subscription, document)
            if subscription is not None:
                subscriptions.append(subscription)
        return subscriptions

    async def list_reminder_subscriptions(
        self,
        reminder_start_at,
        reminder_end_at,
        limit: int,
    ) -> list[Subscription]:
        cursor = (
            self._subscriptions.find(
                {
                    "status": "active",
                    "next_billing_at": {
                        "$gte": reminder_start_at,
                        "$lte": reminder_end_at,
                    },
                },
                session=self._session,
            )
            .sort("next_billing_at", 1)
            .limit(limit)
        )
        subscriptions: list[Subscription] = []
        async for document in cursor:
            subscription = from_document(Subscription, document)
            if subscription is not None:
                subscriptions.append(subscription)
        return subscriptions

    async def count_excluded_billing_subscriptions(self) -> int:
        return await self._subscriptions.count_documents(
            {
                "$or": [
                    {"status": "cancel_scheduled"},
                    {"status": "active", "next_billing_at": None},
                ]
            },
            session=self._session,
        )

    async def get_subscription_plan(
        self,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        return from_document(
            SubscriptionPlan,
            await self._subscription_plans.find_one(
                {"_id": plan_id},
                session=self._session,
            ),
        )

    async def get_invoice_by_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Invoice | None:
        return from_document(
            Invoice,
            await self._invoices.find_one(
                {
                    "subscription_id": subscription_id,
                    "billing_cycle_key": billing_cycle_key,
                    "status": {"$in": ["issued", "paid"]},
                },
                session=self._session,
            ),
        )

    async def get_invoice(self, invoice_id: str) -> Invoice | None:
        return from_document(
            Invoice,
            await self._invoices.find_one(
                {"_id": invoice_id},
                session=self._session,
            ),
        )

    async def get_payment(self, payment_id: str) -> Payment | None:
        return from_document(
            Payment,
            await self._payments.find_one(
                {"_id": payment_id},
                session=self._session,
            ),
        )

    async def get_latest_failed_payment_for_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Payment | None:
        cursor = (
            self._payments.find(
                {
                    "subscription_id": subscription_id,
                    "billing_cycle_key": billing_cycle_key,
                    "status": "failed",
                },
                session=self._session,
            )
            .sort([("created_at", -1), ("_id", -1)])
            .limit(1)
        )
        async for document in cursor:
            return from_document(Payment, document)
        return None

    async def count_failed_payments_for_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> int:
        return await self._payments.count_documents(
            {
                "subscription_id": subscription_id,
                "billing_cycle_key": billing_cycle_key,
                "status": "failed",
            },
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

    async def get_default_billing_method(self, user_id: str) -> BillingMethod | None:
        return from_document(
            BillingMethod,
            await self._billing_methods.find_one(
                {"user_id": user_id, "is_default": True, "status": "active"},
                session=self._session,
            ),
        )

    async def get_payment_instrument(
        self,
        instrument_id: str,
    ) -> PaymentInstrument | None:
        return from_document(
            PaymentInstrument,
            await self._payment_instruments.find_one(
                {"_id": instrument_id},
                session=self._session,
            ),
        )

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

    async def save_subscription(self, subscription: Subscription) -> None:
        await self._subscriptions.replace_one(
            {"_id": subscription.id},
            to_document(subscription),
            upsert=True,
            session=self._session,
        )

    async def save_subscription_billing_result(
        self,
        *,
        payment: Payment,
        invoice: Invoice,
        subscription: Subscription,
        expected_next_billing_at,
    ) -> bool:
        result = await self._subscriptions.replace_one(
            {
                "_id": subscription.id,
                "status": "active",
                "next_billing_at": expected_next_billing_at,
            },
            to_document(subscription),
            upsert=False,
            session=self._session,
        )
        if result.matched_count != 1:
            return False
        await self.save_payment(payment)
        await self.save_invoice(invoice)
        return True
