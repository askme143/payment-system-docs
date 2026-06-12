from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from typing import cast

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import to_document
from payments.application.cursors import decode_cursor
from payments.application.errors import BadRequestError
from payments.application.ports.invoices import (
    InvoiceDetailRecord,
    InvoiceListRecord,
    InvoiceRepository,
    InvoiceStatus,
    PaymentStatus,
    SubscriptionStatus,
)
from payments.domain.entities.invoice import Invoice

MongoDocument = dict[str, object]


class MongoInvoiceRepository(InvoiceRepository):
    def __init__(
        self,
        invoices: AsyncIOMotorCollection,
        payments: AsyncIOMotorCollection,
        subscriptions: AsyncIOMotorCollection,
        subscription_plans: AsyncIOMotorCollection,
        products: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._invoices = invoices
        self._payments = payments
        self._subscriptions = subscriptions
        self._subscription_plans = subscription_plans
        self._products = products
        self._session = session

    async def list_invoices_for_user(
        self,
        user_id: str,
        limit: int,
        status: InvoiceStatus | None = None,
        payment_status: PaymentStatus | None = None,
        subscription_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        cursor: str | None = None,
    ) -> list[InvoiceListRecord]:
        query: dict[str, object] = {"user_id": user_id}
        if status is not None:
            query["status"] = status
        if subscription_id is not None:
            query["subscription_id"] = subscription_id
        issued_at_filter: dict[str, object] = {}
        if from_date is not None:
            issued_at_filter["$gte"] = _start_of_day(from_date)
        if to_date is not None:
            issued_at_filter["$lte"] = _end_of_day(to_date)
        if issued_at_filter:
            query["issued_at"] = issued_at_filter
        if cursor is not None:
            payload = decode_cursor(cursor)
            billing_date = _cursor_date(payload, "billingDate")
            invoice_id = _cursor_string(payload, "invoiceId")
            query = _and_query(
                query,
                {
                    "$or": [
                        {"issued_at": {"$lt": _start_of_day(billing_date)}},
                        {
                            "issued_at": {
                                "$gte": _start_of_day(billing_date),
                                "$lte": _end_of_day(billing_date),
                            },
                            "_id": {"$lt": invoice_id},
                        },
                    ]
                },
            )
        invoice_cursor = (
            self._invoices.find(query, session=self._session)
            .sort([("issued_at", -1), ("_id", -1)])
        )
        if payment_status is None:
            invoice_cursor = invoice_cursor.limit(limit)
        records: list[InvoiceListRecord] = []
        async for invoice in invoice_cursor:
            record = await self._invoice_list_record(invoice)
            if payment_status is not None and record.payment_status != payment_status:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    async def get_invoice_detail_for_user(
        self,
        invoice_id: str,
        user_id: str,
    ) -> InvoiceDetailRecord | None:
        invoice = await self._invoices.find_one(
            {"_id": invoice_id, "user_id": user_id},
            session=self._session,
        )
        if invoice is None:
            return None
        payment = await self._payment_document(invoice)
        failure = _payment_failure(payment)
        failed_retry_scheduled_at = _failed_retry_scheduled_at(payment)
        return InvoiceDetailRecord(
            invoice_id=str(invoice["_id"]),
            subscription_id=_optional_str(invoice.get("subscription_id")),
            subscription_status=await self._subscription_status(invoice),
            status=_invoice_status(invoice),
            payment_status=_payment_status(payment),
            amount=_int(invoice.get("amount") or (payment or {}).get("amount")),
            currency=str(invoice.get("currency", "KRW")),
            billing_date=_date(invoice.get("billing_date") or invoice.get("issued_at")),
            paid_at=_optional_datetime(
                invoice.get("paid_at") or (payment or {}).get("approved_at")
            ),
            receipt_url=_optional_str(
                invoice.get("receipt_url") or (payment or {}).get("receipt_url")
            ),
            failure_code=_failure_code(failure, payment),
            failure_reason=_optional_str(failure.get("reason")),
            failure_message=_failure_message(failure, payment),
            failure_retryable=_failure_retryable(failure, payment),
            retry_available=failed_retry_scheduled_at is not None,
            retry_scheduled_at=failed_retry_scheduled_at,
        )

    async def get_invoice_owner(self, invoice_id: str) -> str | None:
        invoice = await self._invoices.find_one(
            {"_id": invoice_id},
            session=self._session,
        )
        if invoice is None:
            return None
        user_id = invoice.get("user_id")
        return user_id if isinstance(user_id, str) else None

    async def save_invoice(self, invoice: Invoice) -> None:
        await self._invoices.replace_one(
            {"_id": invoice.id},
            to_document(invoice, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def _invoice_list_record(
        self,
        invoice: MongoDocument,
    ) -> InvoiceListRecord:
        payment = await self._payment_document(invoice)
        failure = _payment_failure(payment)
        product_name, plan_name = await self._display_names(invoice)
        receipt_url = invoice.get("receipt_url") or (payment or {}).get("receipt_url")
        return InvoiceListRecord(
            invoice_id=str(invoice["_id"]),
            subscription_id=_optional_str(invoice.get("subscription_id")),
            product_name=product_name,
            plan_name=plan_name,
            invoice_type=str(invoice.get("invoice_type", "recurring")),
            status=_invoice_status(invoice),
            payment_status=_payment_status(payment),
            amount=_int(invoice.get("amount") or (payment or {}).get("amount")),
            currency=str(invoice.get("currency", "KRW")),
            billing_date=_date(invoice.get("billing_date") or invoice.get("issued_at")),
            paid_at=_optional_datetime(
                invoice.get("paid_at") or (payment or {}).get("approved_at")
            ),
            receipt_available=receipt_url is not None,
            failure_summary=_failure_message(failure, payment),
        )

    async def _payment_document(self, invoice: MongoDocument) -> MongoDocument | None:
        latest_failed_payment = await self._latest_failed_payment_document(invoice)
        if latest_failed_payment is not None:
            return latest_failed_payment
        payment_id = invoice.get("payment_id")
        if not isinstance(payment_id, str):
            return None
        return await self._payments.find_one({"_id": payment_id}, session=self._session)

    async def _latest_failed_payment_document(
        self,
        invoice: MongoDocument,
    ) -> MongoDocument | None:
        if _invoice_status(invoice) != "issued":
            return None
        subscription_id = invoice.get("subscription_id")
        if not isinstance(subscription_id, str):
            return None
        billing_cycle_key = invoice.get("billing_cycle_key")
        if not isinstance(billing_cycle_key, str):
            payment = await self._linked_payment_document(invoice)
            billing_cycle_key = (
                payment.get("billing_cycle_key") if payment is not None else None
            )
            if not isinstance(billing_cycle_key, str):
                return None
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
        async for payment in cursor:
            return payment
        return None

    async def _linked_payment_document(
        self,
        invoice: MongoDocument,
    ) -> MongoDocument | None:
        payment_id = invoice.get("payment_id")
        if not isinstance(payment_id, str):
            return None
        return await self._payments.find_one({"_id": payment_id}, session=self._session)

    async def _subscription_status(
        self,
        invoice: MongoDocument,
    ) -> SubscriptionStatus | None:
        subscription_id = invoice.get("subscription_id")
        if not isinstance(subscription_id, str):
            return None
        subscription = await self._subscriptions.find_one(
            {"_id": subscription_id},
            session=self._session,
        )
        if subscription is None:
            return None
        return _subscription_status(subscription)

    async def _display_names(self, invoice: MongoDocument) -> tuple[str, str]:
        subscription_id = invoice.get("subscription_id")
        if not isinstance(subscription_id, str):
            return "", ""
        subscription = await self._subscriptions.find_one(
            {"_id": subscription_id},
            session=self._session,
        )
        if subscription is None:
            return "", ""
        plan_id = subscription.get("plan_id")
        if not isinstance(plan_id, str):
            return "", ""
        plan = await self._subscription_plans.find_one(
            {"_id": plan_id},
            session=self._session,
        )
        if plan is None:
            return "", ""
        product = await self._products.find_one(
            {"_id": plan.get("product_id")},
            session=self._session,
        )
        product_name = str((product or {}).get("name", ""))
        configured_plan_name = plan.get("name")
        if isinstance(configured_plan_name, str) and configured_plan_name:
            return product_name, configured_plan_name
        period = "월간" if plan.get("billing_period") == "monthly" else "연간"
        return product_name, f"{product_name} {period}".strip()


def _invoice_status(invoice: MongoDocument) -> InvoiceStatus:
    status = invoice.get("status")
    if status in {"issued", "paid", "voided", "refunded"}:
        return cast(InvoiceStatus, status)
    return "issued"


def _payment_status(payment: MongoDocument | None) -> PaymentStatus | None:
    if payment is None:
        return None
    status = payment.get("status")
    if status in {
        "ready",
        "paid",
        "failed",
        "expired",
        "canceled",
        "partial_canceled",
    }:
        return cast(PaymentStatus, status)
    return None


def _subscription_status(subscription: MongoDocument) -> SubscriptionStatus | None:
    status = subscription.get("status")
    if status in {"pending", "active", "past_due", "cancel_scheduled", "canceled"}:
        return cast(SubscriptionStatus, status)
    return None


def _payment_failure(payment: MongoDocument | None) -> Mapping[str, object]:
    if payment is None:
        return {}
    failure = payment.get("failure")
    return failure if isinstance(failure, Mapping) else {}


def _failure_code(
    failure: Mapping[str, object],
    payment: MongoDocument | None,
) -> str | None:
    return (
        _optional_str(failure.get("providerCode"))
        or _optional_str(failure.get("code"))
        or _optional_str((payment or {}).get("failure_code"))
    )


def _failure_message(
    failure: Mapping[str, object],
    payment: MongoDocument | None,
) -> str | None:
    return _optional_str(failure.get("message")) or _optional_str(
        (payment or {}).get("failure_message")
    )


def _failure_retryable(
    failure: Mapping[str, object],
    payment: MongoDocument | None,
) -> bool:
    value = failure.get("retryable", (payment or {}).get("failure_retryable", False))
    return value if isinstance(value, bool) else False


def _failed_retry_scheduled_at(payment: MongoDocument | None) -> datetime | None:
    if _payment_status(payment) != "failed":
        return None
    return _optional_datetime((payment or {}).get("retry_scheduled_at"))


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date(1970, 1, 1)


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=UTC)


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _and_query(
    base_query: dict[str, object],
    condition: dict[str, object],
) -> dict[str, object]:
    if not base_query:
        return condition
    return {"$and": [base_query, condition]}


def _cursor_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise BadRequestError("cursor is invalid")
    return value


def _cursor_date(payload: Mapping[str, object], name: str) -> date:
    value = _cursor_string(payload, name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequestError("cursor is invalid") from exc
