from __future__ import annotations

from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document
from payments.application.ports.billing_methods import (
    BillingKeyStatus,
    BillingMethodRecord,
    BillingMethodRepository,
)
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.payment_instrument import PaymentInstrument

_HOLDING_SUBSCRIPTION_STATUSES = [
    "pending",
    "active",
    "past_due",
    "cancel_scheduled",
]


class MongoBillingMethodRepository(BillingMethodRepository):
    def __init__(
        self,
        billing_methods: AsyncIOMotorCollection,
        subscriptions: AsyncIOMotorCollection,
        payment_instruments: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._billing_methods = billing_methods
        self._subscriptions = subscriptions
        self._payment_instruments = payment_instruments
        self._session = session

    async def list_active_billing_methods_for_user(
        self,
        user_id: str,
    ) -> list[BillingMethodRecord]:
        cursor = self._billing_methods.find(
            {"user_id": user_id, "status": "active"},
            session=self._session,
        )
        records: list[BillingMethodRecord] = []
        async for document in cursor:
            billing_method = from_document(BillingMethod, document)
            if billing_method is None:
                continue
            records.append(await self._billing_method_record(billing_method))
        return records

    async def count_active_subscriptions_for_user(self, user_id: str) -> int:
        return await self._subscriptions.count_documents(
            {"user_id": user_id, "status": {"$in": _HOLDING_SUBSCRIPTION_STATUSES}},
            session=self._session,
        )

    async def get_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
    ) -> BillingMethodRecord | None:
        document = await self._billing_methods.find_one(
            {"_id": billing_method_id, "user_id": user_id, "status": "active"},
            session=self._session,
        )
        billing_method = from_document(BillingMethod, document)
        if billing_method is None:
            return None
        return await self._billing_method_record(billing_method)

    async def get_any_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
    ) -> BillingMethodRecord | None:
        document = await self._billing_methods.find_one(
            {"_id": billing_method_id, "user_id": user_id},
            session=self._session,
        )
        billing_method = from_document(BillingMethod, document)
        if billing_method is None:
            return None
        return await self._billing_method_record(billing_method)

    async def _billing_method_record(
        self,
        billing_method: BillingMethod,
    ) -> BillingMethodRecord:
        instrument_document = await self._payment_instruments.find_one(
            {"_id": billing_method.instrument_id},
            session=self._session,
        )
        instrument = from_document(PaymentInstrument, instrument_document)
        billing_key_status: BillingKeyStatus = (
            instrument.status if instrument is not None else "revoked"
        )
        return _billing_method_record(
            billing_method,
            billing_key_status=billing_key_status,
        )

    async def get_billing_method_owner(self, billing_method_id: str) -> str | None:
        document = await self._billing_methods.find_one(
            {"_id": billing_method_id},
            session=self._session,
        )
        if document is None:
            return None
        user_id = document.get("user_id")
        return user_id if isinstance(user_id, str) else None

    async def set_default_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
        changed_at: datetime,
    ) -> str | None:
        target_method = await self._billing_methods.find_one(
            {
                "_id": billing_method_id,
                "user_id": user_id,
                "status": "active",
            },
            session=self._session,
        )
        if target_method is None:
            raise LookupError("billing method was not defaultable")
        instrument_id = target_method.get("instrument_id")
        target_instrument = await self._payment_instruments.find_one(
            {
                "_id": instrument_id,
                "status": "active",
            },
            session=self._session,
        )
        if target_instrument is None:
            raise LookupError("billing method was not defaultable")
        previous_default = await self._billing_methods.find_one(
            {"user_id": user_id, "is_default": True, "status": "active"},
            session=self._session,
        )
        previous_default_id = (
            str(previous_default["_id"]) if previous_default is not None else None
        )
        await self._billing_methods.update_many(
            {"user_id": user_id, "is_default": True, "status": "active"},
            {"$set": {"is_default": False}},
            session=self._session,
        )
        target_update = await self._billing_methods.update_one(
            {
                "_id": billing_method_id,
                "user_id": user_id,
                "status": "active",
            },
            {
                "$set": {
                    "is_default": True,
                    "default_changed_at": changed_at,
                },
                "$unset": {"billing_key_status": ""},
            },
            session=self._session,
        )
        if target_update.matched_count != 1:
            raise LookupError("billing method was not defaultable")
        return previous_default_id

    async def deactivate_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
        deleted_at: datetime,
    ) -> None:
        method = await self._billing_methods.find_one(
            {"_id": billing_method_id, "user_id": user_id, "status": "active"},
            session=self._session,
        )
        instrument_id = method.get("instrument_id") if method is not None else None
        result = await self._billing_methods.update_one(
            {"_id": billing_method_id, "user_id": user_id, "status": "active"},
            {
                "$set": {
                    "status": "inactive",
                    "deleted_at": deleted_at,
                    "is_default": False,
                },
                "$unset": {"billing_key_status": ""},
            },
            session=self._session,
        )
        if result.matched_count != 1:
            raise LookupError("billing method was not deletable")
        if instrument_id is not None:
            await self._payment_instruments.update_one(
                {"_id": instrument_id},
                {"$set": {"status": "revoked", "revoked_at": deleted_at}},
                session=self._session,
            )


def _billing_method_record(
    billing_method: BillingMethod,
    *,
    billing_key_status: BillingKeyStatus,
) -> BillingMethodRecord:
    return BillingMethodRecord(
        billing_method_id=billing_method.id,
        status=billing_method.status,
        is_default=billing_method.is_default,
        method=billing_method.method,
        card_company=billing_method.card_company,
        masked_card_number=billing_method.masked_number
        or _masked_number_from_display_name(billing_method.display_name),
        billing_key_status=billing_key_status,
        created_at=billing_method.created_at
        or datetime(1970, 1, 1, tzinfo=UTC),
    )


def _masked_number_from_display_name(display_name: str) -> str:
    if "****" in display_name:
        return display_name.removeprefix("카드 ").strip()
    return ""
