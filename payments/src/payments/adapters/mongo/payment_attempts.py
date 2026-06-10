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
            to_document(payment),
            upsert=True,
            session=self._session,
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
