from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.ports.payment_customers import PaymentCustomerRepository
from payments.domain.entities.payment_customer import PaymentCustomer


class MongoPaymentCustomerRepository(PaymentCustomerRepository):
    def __init__(
        self,
        payment_customers: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._payment_customers = payment_customers
        self._session = session

    async def get_active_payment_customer_for_user(
        self,
        user_id: str,
    ) -> PaymentCustomer | None:
        document = await self._payment_customers.find_one(
            {
                "user_id": user_id,
                "provider": "tosspayments",
                "status": "active",
            },
            session=self._session,
        )
        return from_document(PaymentCustomer, document)

    async def save_payment_customer(self, payment_customer: PaymentCustomer) -> None:
        await self._payment_customers.replace_one(
            {"_id": payment_customer.id},
            to_document(payment_customer, omit_none=True),
            upsert=True,
            session=self._session,
        )
