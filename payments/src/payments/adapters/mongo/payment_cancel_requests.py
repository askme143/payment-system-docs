from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.ports.payment_cancel_requests import (
    PaymentCancelRequestRepository,
)
from payments.domain.entities.payment_cancel_request import PaymentCancelRequest


class MongoPaymentCancelRequestRepository(PaymentCancelRequestRepository):
    def __init__(
        self,
        payment_cancel_requests: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._payment_cancel_requests = payment_cancel_requests
        self._session = session

    async def find_payment_cancel_request(
        self,
        payment_id: str,
        idempotency_key_hash: str,
    ) -> PaymentCancelRequest | None:
        document = await self._payment_cancel_requests.find_one(
            {
                "payment_id": payment_id,
                "idempotency_key_hash": idempotency_key_hash,
            },
            session=self._session,
        )
        return from_document(PaymentCancelRequest, document)

    async def save_payment_cancel_request(
        self,
        payment_cancel_request: PaymentCancelRequest,
    ) -> None:
        await self._payment_cancel_requests.replace_one(
            {"_id": payment_cancel_request.id},
            to_document(payment_cancel_request, omit_none=True),
            upsert=True,
            session=self._session,
        )
