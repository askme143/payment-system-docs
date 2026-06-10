from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.payment import Payment


class MongoPaymentRepository:
    def __init__(
        self,
        checkouts: AsyncIOMotorCollection,
        payments: AsyncIOMotorCollection,
        idempotency_keys: AsyncIOMotorCollection,
    ) -> None:
        self._checkouts = checkouts
        self._payments = payments
        self._idempotency_keys = idempotency_keys

    async def find_idempotency_key(
        self,
        scope: str,
        key_hash: str,
    ) -> IdempotencyKey | None:
        document = await self._idempotency_keys.find_one(
            {"scope": scope, "key_hash": key_hash}
        )
        return from_document(IdempotencyKey, document)

    async def save_idempotency_key(self, key: IdempotencyKey) -> None:
        await self._idempotency_keys.replace_one(
            {"_id": key.id},
            to_document(key),
            upsert=True,
        )

    async def save_checkout(self, checkout: Checkout) -> None:
        await self._checkouts.replace_one(
            {"_id": checkout.id},
            to_document(checkout),
            upsert=True,
        )

    async def save_payment(self, payment: Payment) -> None:
        await self._payments.replace_one(
            {"_id": payment.id},
            to_document(payment),
            upsert=True,
        )

    async def get_checkout_for_user(
        self, checkout_id: str, user_id: str
    ) -> Checkout | None:
        document = await self._checkouts.find_one(
            {"_id": checkout_id, "user_id": user_id}
        )
        return from_document(Checkout, document)

    async def get_payment_for_user(
        self, payment_id: str, user_id: str
    ) -> Payment | None:
        payment_document = await self._payments.find_one({"_id": payment_id})
        payment = from_document(Payment, payment_document)
        if payment is None or payment.checkout_id is None:
            return None
        checkout = await self.get_checkout_for_user(payment.checkout_id, user_id)
        if checkout is None:
            return None
        return payment
