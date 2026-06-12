from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.ports.checkouts import CheckoutRepository
from payments.domain.entities.checkout import Checkout


class MongoCheckoutRepository(CheckoutRepository):
    def __init__(
        self,
        checkouts: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._checkouts = checkouts
        self._session = session

    async def save_checkout(self, checkout: Checkout) -> None:
        await self._checkouts.replace_one(
            {"_id": checkout.id},
            to_document(checkout, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def get_checkout_for_user(
        self,
        checkout_id: str,
        user_id: str,
    ) -> Checkout | None:
        document = await self._checkouts.find_one(
            {"_id": checkout_id, "user_id": user_id},
            session=self._session,
        )
        return from_document(Checkout, document)

    async def get_checkout(self, checkout_id: str) -> Checkout | None:
        document = await self._checkouts.find_one(
            {"_id": checkout_id},
            session=self._session,
        )
        return from_document(Checkout, document)

    async def mark_checkout_paid_if_ready(
        self,
        checkout_id: str,
        user_id: str,
        last_payment_id: str,
    ) -> bool:
        result = await self._checkouts.update_one(
            {"_id": checkout_id, "user_id": user_id, "status": "ready"},
            {
                "$set": {
                    "status": "paid",
                    "last_payment_id": last_payment_id,
                }
            },
            session=self._session,
        )
        return result.modified_count == 1
