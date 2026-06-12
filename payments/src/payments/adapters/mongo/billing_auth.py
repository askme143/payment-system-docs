from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.domain.entities.billing_auth import BillingAuth
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.payment_instrument import PaymentInstrument


class MongoBillingAuthRepository:
    def __init__(
        self,
        *,
        billing_auths: AsyncIOMotorCollection,
        payment_customers: AsyncIOMotorCollection,
        billing_methods: AsyncIOMotorCollection,
        payment_instruments: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._billing_auths = billing_auths
        self._payment_customers = payment_customers
        self._billing_methods = billing_methods
        self._payment_instruments = payment_instruments
        self._session = session

    async def get_customer_key_for_user(self, user_id: str) -> str | None:
        document = await self._payment_customers.find_one(
            {
                "user_id": user_id,
                "provider": "tosspayments",
                "status": "active",
            },
            session=self._session,
        )
        if document is None:
            return None
        value = document.get("customer_key")
        return value if isinstance(value, str) else None

    async def save_customer_key_for_user(
        self,
        user_id: str,
        customer_key: str,
    ) -> None:
        await self._payment_customers.replace_one(
            {"_id": f"pcus_for_{user_id}"},
            {
                "_id": f"pcus_for_{user_id}",
                "user_id": user_id,
                "provider": "tosspayments",
                "customer_key": customer_key,
                "status": "active",
            },
            upsert=True,
            session=self._session,
        )

    async def count_active_billing_methods_for_user(self, user_id: str) -> int:
        active_method_count = await self._billing_methods.count_documents(
            {"user_id": user_id, "status": "active"},
            session=self._session,
        )
        if active_method_count == 0:
            return 0
        cursor = self._billing_methods.find(
            {"user_id": user_id, "status": "active"},
            session=self._session,
        )
        count = 0
        async for document in cursor:
            instrument_id = document.get("instrument_id")
            instrument = await self._payment_instruments.find_one(
                {"_id": instrument_id, "status": "active"},
                session=self._session,
            )
            if instrument is not None:
                count += 1
        return count

    async def save_billing_auth(self, billing_auth: BillingAuth) -> None:
        document = to_document(billing_auth, omit_none=True)
        for transient_field in ("success_url", "fail_url", "created_at"):
            document.pop(transient_field, None)
        await self._billing_auths.replace_one(
            {"_id": billing_auth.id},
            document,
            upsert=True,
            session=self._session,
        )

    async def get_billing_auth_for_user(
        self,
        billing_auth_id: str,
        user_id: str,
    ) -> BillingAuth | None:
        document = await self._billing_auths.find_one(
            {"_id": billing_auth_id, "user_id": user_id},
            session=self._session,
        )
        return from_document(BillingAuth, document)

    async def clear_default_billing_methods_for_user(self, user_id: str) -> None:
        await self._billing_methods.update_many(
            {"user_id": user_id, "is_default": True, "status": "active"},
            {"$set": {"is_default": False}},
            session=self._session,
        )

    async def save_payment_instrument(
        self,
        instrument: PaymentInstrument,
    ) -> None:
        await self._payment_instruments.replace_one(
            {"_id": instrument.id},
            to_document(instrument, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def save_billing_method(self, billing_method: BillingMethod) -> None:
        document = to_document(billing_method, omit_none=True)
        document.pop("billing_key_status", None)
        await self._billing_methods.replace_one(
            {"_id": billing_method.id},
            document,
            upsert=True,
            session=self._session,
        )
