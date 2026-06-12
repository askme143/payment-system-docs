from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.checkouts import MongoCheckoutRepository
from payments.adapters.mongo.documents import from_document, to_document
from payments.application.ports.payment_attempts import PaymentAttemptRepository
from payments.domain.entities.payment import Payment


class MongoPaymentAttemptRepository(PaymentAttemptRepository):
    def __init__(
        self,
        checkouts: AsyncIOMotorCollection,
        payments: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._checkouts = checkouts
        self._payments = payments
        self._session = session

    async def save_payment(self, payment: Payment) -> None:
        await self._payments.replace_one(
            {"_id": payment.id},
            to_document(payment, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def get_payment(self, payment_id: str) -> Payment | None:
        return from_document(
            Payment,
            await self._payments.find_one(
                {"_id": payment_id},
                session=self._session,
            ),
        )

    async def get_payment_for_user(
        self,
        payment_id: str,
        user_id: str,
    ) -> Payment | None:
        payment_document = await self._payments.find_one(
            {"_id": payment_id},
            session=self._session,
        )
        payment = from_document(Payment, payment_document)
        if payment is None or payment.checkout_id is None:
            return None
        checkout_repository = MongoCheckoutRepository(
            self._checkouts,
            session=self._session,
        )
        checkout = await checkout_repository.get_checkout_for_user(
            payment.checkout_id,
            user_id,
        )
        if checkout is None:
            return None
        return payment

    async def count_payments_for_checkout(self, checkout_id: str) -> int:
        return await self._payments.count_documents(
            {"checkout_id": checkout_id},
            session=self._session,
        )

    async def get_payment_attempt_no(self, checkout_id: str, payment_id: str) -> int:
        documents = []
        cursor = self._payments.find(
            {"checkout_id": checkout_id},
            session=self._session,
        )
        async for document in cursor:
            documents.append(document)
        documents.sort(
            key=lambda document: (
                str(document.get("created_at", "")),
                str(document.get("_id", "")),
            )
        )
        for index, document in enumerate(documents, start=1):
            if document.get("_id") == payment_id:
                return index
        return max(len(documents), 1)

    async def count_user_payment_quantity_for_sku(
        self,
        user_id: str,
        sku_id: str,
        statuses: set[str],
    ) -> int:
        quantity = 0
        cursor = self._payments.find(
            {
                "status": {"$in": list(statuses)},
                "checkout_id": {"$ne": None},
            },
            session=self._session,
        )
        async for payment_document in cursor:
            checkout_id = payment_document.get("checkout_id")
            if not isinstance(checkout_id, str):
                continue
            checkout_document = await self._checkouts.find_one(
                {"_id": checkout_id, "user_id": user_id},
                session=self._session,
            )
            if checkout_document is None:
                continue
            items = checkout_document.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("skuId") != sku_id:
                    continue
                item_quantity = item.get("quantity")
                if isinstance(item_quantity, int):
                    quantity += item_quantity
        return quantity
