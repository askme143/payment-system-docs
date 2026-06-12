from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.cursors import decode_cursor
from payments.application.errors import BadRequestError
from payments.application.ports.admin_operations import (
    AdminListQuery,
    AdminPaymentListRecord,
    AdminSubscriptionListRecord,
)
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class MongoAdminOperationsRepository:
    def __init__(
        self,
        *,
        payments: AsyncIOMotorCollection,
        invoices: AsyncIOMotorCollection,
        checkouts: AsyncIOMotorCollection,
        subscriptions: AsyncIOMotorCollection,
        subscription_plans: AsyncIOMotorCollection,
        products: AsyncIOMotorCollection,
        billing_methods: AsyncIOMotorCollection,
        operator_audits: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._payments = payments
        self._invoices = invoices
        self._checkouts = checkouts
        self._subscriptions = subscriptions
        self._subscription_plans = subscription_plans
        self._products = products
        self._billing_methods = billing_methods
        self._operator_audits = operator_audits
        self._session = session

    async def list_admin_payments(
        self,
        query: AdminListQuery,
    ) -> list[AdminPaymentListRecord]:
        mongo_query: dict[str, object] = {}
        status_values = _query_status_values(query.status)
        if len(status_values) == 1:
            mongo_query["status"] = status_values[0]
        elif len(status_values) > 1:
            mongo_query["status"] = {"$in": list(status_values)}
        if query.order_id:
            mongo_query["order_id"] = query.order_id
        if query.payment_key:
            mongo_query["payment_key"] = query.payment_key
        approved_at_filter: dict[str, object] = {}
        if query.from_at is not None:
            approved_at_filter["$gte"] = query.from_at
        if query.to_at is not None:
            approved_at_filter["$lte"] = query.to_at
        if approved_at_filter:
            mongo_query = _and_query(
                mongo_query,
                {
                    "$or": [
                        {"created_at": approved_at_filter},
                        {"approved_at": approved_at_filter},
                    ]
                },
            )
        cursor_sort_at: datetime | None = None
        cursor_payment_id: str | None = None
        if query.cursor:
            payload = decode_cursor(query.cursor)
            cursor_sort_at = _cursor_sort_datetime(payload)
            cursor_payment_id = _cursor_string(payload, "paymentId")

        rows: list[AdminPaymentListRecord] = []
        cursor = self._payments.find(mongo_query, session=self._session)
        async for payment_document in cursor:
            payment = from_document(Payment, payment_document)
            if payment is None:
                continue
            checkout = await self._get_checkout(payment.checkout_id)
            if query.user_id and (
                checkout is None or checkout.user_id != query.user_id
            ):
                continue
            record = _payment_record(payment, checkout)
            if (
                cursor_sort_at is not None
                and cursor_payment_id is not None
                and not _payment_record_after_cursor(
                    record,
                    cursor_sort_at,
                    cursor_payment_id,
                )
            ):
                continue
            rows.append(record)
        rows.sort(key=_payment_record_sort_key, reverse=True)
        if len(rows) > query.limit:
            rows = rows[: query.limit]
        return rows

    async def list_admin_subscriptions(
        self,
        query: AdminListQuery,
    ) -> list[AdminSubscriptionListRecord]:
        mongo_query: dict[str, object] = {}
        status_values = _query_status_values(query.status)
        if len(status_values) == 1:
            mongo_query["status"] = status_values[0]
        elif len(status_values) > 1:
            mongo_query["status"] = {"$in": list(status_values)}
        if query.user_id:
            mongo_query["user_id"] = query.user_id
        if query.product_code:
            mongo_query["product_code"] = query.product_code
        next_billing_filter: dict[str, object] = {}
        if query.next_billing_from is not None:
            next_billing_filter["$gte"] = query.next_billing_from
        if query.next_billing_to is not None:
            next_billing_filter["$lte"] = query.next_billing_to
        if next_billing_filter:
            mongo_query["next_billing_at"] = next_billing_filter
        cursor_next_billing_at: datetime | None = None
        cursor_subscription_id: str | None = None
        if query.cursor:
            payload = decode_cursor(query.cursor)
            cursor_next_billing_at = _cursor_optional_datetime(
                payload,
                "nextBillingAt",
            )
            cursor_subscription_id = _cursor_string(payload, "subscriptionId")

        rows: list[AdminSubscriptionListRecord] = []
        cursor = (
            self._subscriptions.find(mongo_query)
            if self._session is None
            else self._subscriptions.find(mongo_query, session=self._session)
        )
        async for subscription_document in cursor:
            subscription = from_document(Subscription, subscription_document)
            if subscription is None:
                continue
            plan = await self._get_plan(subscription.plan_id)
            product = await self._get_product(plan.product_id) if plan else None
            billing_method = await self._get_default_billing_method(
                subscription.user_id
            )
            failed_payment, failed_invoice = (
                await self.get_admin_latest_failed_subscription_payment(
                    subscription.id
                )
                if subscription.status == "past_due"
                or query.payment_failure is not None
                else (None, None)
            )
            record = _subscription_record(
                subscription,
                plan,
                product,
                billing_method,
                failed_payment,
                failed_invoice,
            )
            if query.payment_failure is not None and (
                _has_payment_failure(record.payment_failure) != query.payment_failure
            ):
                continue
            if cursor_subscription_id is not None and not (
                _subscription_record_after_cursor(
                    record,
                    cursor_next_billing_at,
                    cursor_subscription_id,
                )
            ):
                continue
            rows.append(record)
        rows.sort(key=_subscription_record_sort_key)
        if len(rows) > query.limit:
            rows = rows[: query.limit]
        return rows

    async def get_admin_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        document = await self._subscriptions.find_one(
            {"_id": subscription_id},
            session=self._session,
        )
        return from_document(Subscription, document)

    async def get_admin_subscription_plan(
        self,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        return await self._get_plan(plan_id)

    async def get_admin_payment_by_invoice_id(
        self,
        invoice_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        invoice = from_document(
            Invoice,
            await self._invoices.find_one(
                {"_id": invoice_id},
                session=self._session,
            ),
        )
        if invoice is None:
            return (None, None)
        payment = from_document(
            Payment,
            await self._payments.find_one(
                {"_id": invoice.payment_id},
                session=self._session,
            ),
        )
        return (payment, invoice)

    async def get_admin_payment_by_payment_key(
        self,
        payment_key: str,
    ) -> Payment | None:
        return from_document(
            Payment,
            await self._payments.find_one(
                {"payment_key": payment_key},
                session=self._session,
            ),
        )

    async def get_admin_invoice_by_payment_id(
        self,
        payment_id: str,
    ) -> Invoice | None:
        return from_document(
            Invoice,
            await self._invoices.find_one(
                {"payment_id": payment_id},
                session=self._session,
            ),
        )

    async def get_admin_latest_failed_subscription_payment(
        self,
        subscription_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        cursor = (
            self._payments.find(
                {"subscription_id": subscription_id, "status": "failed"},
                session=self._session,
            )
            .sort([("created_at", -1), ("_id", -1)])
            .limit(1)
        )
        async for document in cursor:
            payment = from_document(Payment, document)
            if payment is None:
                return (None, None)
            return (
                payment,
                await self.get_admin_invoice_by_payment_id(payment.id),
            )
        return (None, None)

    async def save_admin_subscription(self, subscription: Subscription) -> None:
        await self._subscriptions.replace_one(
            {"_id": subscription.id},
            to_document(subscription),
            upsert=True,
            session=self._session,
        )

    async def save_admin_payment(self, payment: Payment) -> None:
        await self._payments.replace_one(
            {"_id": payment.id},
            to_document(payment, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def save_admin_invoice(self, invoice: Invoice) -> None:
        await self._invoices.replace_one(
            {"_id": invoice.id},
            to_document(invoice, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def save_subscription_adjustment_audit_record(
        self,
        *,
        audit_id: str,
        subscription_id: str,
        admin_id: str,
        request_id: str,
        adjustment_type: str,
        reason_code: str,
        reason_message: str,
        previous: dict[str, object],
        next_value: dict[str, object],
        notified_customer: bool,
        request_ip: str | None = None,
        result: str = "succeeded",
        idempotency_key_id: str | None = None,
        idempotency_scope: str | None = None,
        idempotency_key_hash: str | None = None,
        idempotency_request_hash: str | None = None,
    ) -> None:
        await self._operator_audits.replace_one(
            {"_id": audit_id},
            {
                "_id": audit_id,
                "operator_id": admin_id,
                "action": "subscription.adjust",
                "adjustment_type": adjustment_type,
                "target_type": "subscription",
                "target_id": subscription_id,
                "idempotency_key_id": idempotency_key_id,
                "idempotency_scope": idempotency_scope,
                "idempotency_key_hash": idempotency_key_hash,
                "idempotency_request_hash": idempotency_request_hash,
                "reason_code": reason_code,
                "reason_message": reason_message,
                "request_ip": request_ip,
                "previous_state": previous,
                "next_state": {
                    **next_value,
                    "adjustment_type": adjustment_type,
                    "notified_customer": notified_customer,
                },
                "result": result,
                "created_at": datetime.now(UTC),
                "notified_customer": notified_customer,
            },
            upsert=True,
            session=self._session,
        )

    async def save_admin_list_audit_record(
        self,
        *,
        audit_id: str,
        admin_id: str,
        request_id: str,
        action: str,
        target_type: str,
        target_id: str,
        query: dict[str, object],
        result_count: int,
        has_more: bool,
        request_ip: str | None,
        created_at: datetime,
    ) -> None:
        await self._operator_audits.replace_one(
            {"_id": audit_id},
            {
                "_id": audit_id,
                "operator_id": admin_id,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "request_id": request_id,
                "request_ip": request_ip,
                "previous_state": {},
                "next_state": {
                    "query": query,
                    "result_count": result_count,
                    "has_more": has_more,
                },
                "reason_code": "admin_list_query",
                "result": "succeeded",
                "created_at": created_at,
            },
            upsert=True,
            session=self._session,
        )

    async def _get_checkout(self, checkout_id: str | None) -> Checkout | None:
        if checkout_id is None:
            return None
        return from_document(
            Checkout,
            await self._checkouts.find_one(
                {"_id": checkout_id},
                session=self._session,
            ),
        )

    async def _get_plan(self, plan_id: str) -> SubscriptionPlan | None:
        return from_document(
            SubscriptionPlan,
            await self._subscription_plans.find_one(
                {"_id": plan_id},
                session=self._session,
            ),
        )

    async def _get_product(self, product_id: str) -> Product | None:
        return from_document(
            Product,
            await self._products.find_one(
                {"_id": product_id},
                session=self._session,
            ),
        )

    async def _get_default_billing_method(
        self,
        user_id: str,
    ) -> BillingMethod | None:
        return from_document(
            BillingMethod,
            await self._billing_methods.find_one(
                {"user_id": user_id, "is_default": True, "status": "active"},
                session=self._session,
            ),
        )


def _payment_record(
    payment: Payment,
    checkout: Checkout | None,
) -> AdminPaymentListRecord:
    cancelable_amount = payment.cancelable_amount
    if cancelable_amount is None:
        canceled_amount = sum(
            int(cancel.get("cancelAmount", 0))
            for cancel in (payment.cancel_history or [])
        )
        cancelable_amount = max(payment.amount - canceled_amount, 0)
    return AdminPaymentListRecord(
        payment_id=payment.id,
        checkout_id=payment.checkout_id,
        user_id=checkout.user_id if checkout else None,
        user_email=None,
        order_id=payment.order_id,
        order_name=_order_name(checkout),
        payment_key=payment.payment_key,
        status=payment.status,
        amount=payment.amount,
        paid_amount=(
            payment.amount
            if payment.status in {"paid", "canceled", "partial_canceled"}
            else 0
        ),
        cancelable_amount=cancelable_amount,
        currency="KRW",
        created_at=payment.created_at,
        approved_at=payment.approved_at,
        method_summary=_method_summary(payment),
    )


def _subscription_record(
    subscription: Subscription,
    plan: SubscriptionPlan | None,
    product: Product | None,
    billing_method: BillingMethod | None,
    failed_payment: Payment | None,
    failed_invoice: Invoice | None,
) -> AdminSubscriptionListRecord:
    product_name = product.name if product else subscription.product_code
    plan_name = _plan_display_name(product, plan) if product and plan else plan_id_name(
        subscription.plan_id
    )
    return AdminSubscriptionListRecord(
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        user_email=None,
        product_code=subscription.product_code,
        product_name=product_name,
        plan_id=subscription.plan_id,
        plan_name=plan_name,
        status=subscription.status,
        current_period_start_at=subscription.current_period_start_at,
        current_period_end_at=subscription.current_period_end_at,
        next_billing_at=subscription.next_billing_at,
        payment_failure=_subscription_payment_failure(
            subscription,
            failed_payment,
            failed_invoice,
        ),
        default_billing_method_summary=_billing_method_summary(billing_method),
    )


def _order_name(checkout: Checkout | None) -> str:
    if checkout is None or not checkout.items:
        return "Payment"
    return f"{len(checkout.items)} item order"


def _method_summary(payment: Payment) -> str | None:
    if payment.method_detail and "maskedCardNumber" in payment.method_detail:
        masked_number = str(payment.method_detail["maskedCardNumber"])
        return f"{payment.method or 'card'} {masked_number[-4:]}"
    return payment.method


def _payment_record_sort_key(record: AdminPaymentListRecord) -> tuple[datetime, str]:
    return (record.approved_at or record.created_at, record.payment_id)


def _payment_record_after_cursor(
    record: AdminPaymentListRecord,
    cursor_sort_at: datetime,
    cursor_payment_id: str,
) -> bool:
    sort_at = record.approved_at or record.created_at
    return sort_at < cursor_sort_at or (
        sort_at == cursor_sort_at and record.payment_id < cursor_payment_id
    )


def _billing_method_summary(billing_method: BillingMethod | None) -> str | None:
    if billing_method is None:
        return None
    method = billing_method.method or "카드"
    last_four = _last_four_digits(billing_method.masked_number) or _last_four_digits(
        billing_method.display_name
    )
    if last_four is None:
        return method
    return f"{method} {last_four}"


def _last_four_digits(value: str | None) -> str | None:
    digits = "".join(character for character in value or "" if character.isdigit())
    if len(digits) < 4:
        return None
    return digits[-4:]


def _subscription_record_sort_key(
    record: AdminSubscriptionListRecord,
) -> tuple[bool, datetime, str]:
    return (
        record.next_billing_at is None,
        record.next_billing_at or datetime.max.replace(tzinfo=UTC),
        record.subscription_id,
    )


def _subscription_record_after_cursor(
    record: AdminSubscriptionListRecord,
    cursor_next_billing_at: datetime | None,
    cursor_subscription_id: str,
) -> bool:
    return _subscription_record_sort_key(record) > (
        cursor_next_billing_at is None,
        cursor_next_billing_at or datetime.max.replace(tzinfo=UTC),
        cursor_subscription_id,
    )


def _subscription_payment_failure(
    subscription: Subscription,
    failed_payment: Payment | None,
    failed_invoice: Invoice | None,
) -> dict[str, object]:
    has_open_failure_invoice = (
        failed_invoice is not None
        and failed_invoice.status == "issued"
        and failed_payment is not None
        and failed_payment.retry_scheduled_at is not None
    )
    has_failure = subscription.status == "past_due" or has_open_failure_invoice
    if not has_failure:
        return {"hasFailure": False}
    failure = failed_payment.failure if failed_payment is not None else None
    return {
        "hasFailure": True,
        "lastInvoiceId": failed_invoice.id if failed_invoice is not None else None,
        "failureCode": _failure_code(failure, failed_payment),
        "retryScheduledAt": (
            failed_payment.retry_scheduled_at if failed_payment is not None else None
        ),
        "retryAvailable": _failure_retryable(failure),
    }


def _query_status_values(
    status: str | tuple[str, ...] | None,
) -> tuple[str, ...]:
    if status is None:
        return ()
    values = (status,) if isinstance(status, str) else status
    return tuple(
        value.strip()
        for item in values
        for value in item.split(",")
        if value.strip()
    )


def _has_payment_failure(payment_failure: dict[str, object] | None) -> bool:
    return bool(payment_failure and payment_failure.get("hasFailure"))


def _failure_retryable(failure: Mapping[str, object] | None) -> bool:
    return bool(failure and failure.get("retryable"))


def _failure_code(
    failure: Mapping[str, object] | None,
    payment: Payment | None,
) -> str | None:
    if failure is None:
        failure = {}
    return (
        _optional_str(failure.get("providerCode"))
        or _optional_str(failure.get("code"))
        or _optional_str(
            payment.provider_response_summary.get("failure_code")
            if payment is not None and payment.provider_response_summary is not None
            else None
        )
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _plan_display_name(product: Product, plan: SubscriptionPlan) -> str:
    period_label = "monthly" if plan.billing_period == "monthly" else "yearly"
    return f"{product.name} {period_label}"


def plan_id_name(plan_id: str) -> str:
    return plan_id


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


def _cursor_datetime(payload: Mapping[str, object], name: str) -> datetime:
    value = _cursor_string(payload, name)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequestError("cursor is invalid") from exc


def _cursor_optional_datetime(
    payload: Mapping[str, object],
    name: str,
) -> datetime | None:
    value = payload.get(name)
    if value is None:
        if payload.get(f"{name}Null") is True:
            return None
        raise BadRequestError("cursor is invalid")
    if not isinstance(value, str) or not value:
        raise BadRequestError("cursor is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequestError("cursor is invalid") from exc


def _cursor_sort_datetime(payload: Mapping[str, object]) -> datetime:
    value = payload.get("sortAt") or payload.get("approvedAt")
    if not isinstance(value, str) or not value:
        raise BadRequestError("cursor is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequestError("cursor is invalid") from exc
