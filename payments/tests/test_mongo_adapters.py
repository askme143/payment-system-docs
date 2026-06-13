from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from payments.adapters.mongo.admin_catalog import MongoAdminCatalogRepository
from payments.adapters.mongo.admin_operations import MongoAdminOperationsRepository
from payments.adapters.mongo.billing_auth import MongoBillingAuthRepository
from payments.adapters.mongo.billing_methods import MongoBillingMethodRepository
from payments.adapters.mongo.billing_retry import MongoBillingRetryRepository
from payments.adapters.mongo.catalog import MongoCatalogRepository
from payments.adapters.mongo.checkouts import MongoCheckoutRepository
from payments.adapters.mongo.idempotency import MongoIdempotencyKeyRepository
from payments.adapters.mongo.indexes import ensure_mongo_indexes
from payments.adapters.mongo.invoices import MongoInvoiceRepository
from payments.adapters.mongo.notifications import (
    MongoNotificationOutboxRepository,
    MongoNotificationTemplateRepository,
)
from payments.adapters.mongo.one_time_skus import MongoOneTimeSkuRepository
from payments.adapters.mongo.operation_locks import MongoOperationLockRepository
from payments.adapters.mongo.payment_attempts import MongoPaymentAttemptRepository
from payments.adapters.mongo.payment_cancel_requests import (
    MongoPaymentCancelRequestRepository,
)
from payments.adapters.mongo.payment_customers import MongoPaymentCustomerRepository
from payments.adapters.mongo.subscriptions import (
    MongoSubscriptionAccountRepository,
    MongoSubscriptionCheckoutRepository,
)
from payments.adapters.mongo.unit_of_work import (
    MongoAdminAuthUnitOfWorkFactory,
    MongoAdminSubscriptionAdjustUnitOfWorkFactory,
    MongoBillingAuthIssueUnitOfWorkFactory,
    MongoBillingMethodDefaultUnitOfWorkFactory,
    MongoBillingMethodDeleteUnitOfWorkFactory,
    MongoSubscriptionBillingUnitOfWorkFactory,
    MongoSubscriptionCancelUnitOfWorkFactory,
    MongoSubscriptionChangeUnitOfWorkFactory,
    MongoSubscriptionExpirationUnitOfWorkFactory,
    MongoSubscriptionResumeUnitOfWorkFactory,
)
from payments.adapters.mongo.webhooks import MongoWebhookRepository
from payments.application.cursors import encode_cursor
from payments.application.errors import InvalidStateTransitionError
from payments.application.ports.admin_operations import AdminListQuery
from payments.domain.entities.admin_auth import AdminAccount, AdminAuthToken
from payments.domain.entities.billing_auth import BillingAuth
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.notification import NotificationLastError
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_cancel_request import PaymentCancelRequest
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan
from payments.domain.entities.webhook_event import WebhookEvent

TestMongoDocument = dict[str, object]


class FakeCursor:
    def __init__(self, documents) -> None:
        self._documents = list(documents)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._documents:
            raise StopAsyncIteration
        return self._documents.pop(0)

    def sort(self, *args, **kwargs):
        return self

    def limit(self, limit):
        self._documents = self._documents[:limit]
        return self


class FakeCollection:
    def __init__(self, documents=None) -> None:
        self.documents = {document["_id"]: document for document in documents or []}
        self.indexes = []
        self.calls = []

    async def create_index(self, keys, **kwargs):
        self.calls.append(("create_index", kwargs))
        self.indexes.append((keys, kwargs))

    def find(self, query, **kwargs):
        self.calls.append(("find", kwargs))
        return FakeCursor(
            document
            for document in self.documents.values()
            if _matches_query(document, query)
        )

    async def find_one(self, query, **kwargs):
        self.calls.append(("find_one", kwargs))
        for document in self.documents.values():
            if _matches_query(document, query):
                return document
        return None

    async def count_documents(self, query, **kwargs):
        self.calls.append(("count_documents", kwargs))
        return sum(
            1
            for document in self.documents.values()
            if _matches_query(document, query)
        )

    async def replace_one(self, query, document, upsert=False, **kwargs):
        self.calls.append(("replace_one", kwargs))
        document_id = query["_id"]
        if document_id in self.documents and not _matches_query(
            self.documents[document_id],
            query,
        ):
            return FakeUpdateResult(modified_count=0, matched_count=0)
        if upsert or document_id in self.documents:
            self.documents[document_id] = document
            return FakeUpdateResult(modified_count=1, matched_count=1)
        return FakeUpdateResult(modified_count=0, matched_count=0)

    async def update_one(self, query, update, **kwargs):
        self.calls.append(("update_one", kwargs))
        document = await self.find_one(
            {key: value for key, value in query.items() if key != "$expr"}
        )
        if document is None or not _matches_stock_expression(document, query):
            return FakeUpdateResult(modified_count=0)
        if isinstance(update, list):
            for stage in update:
                for key, value in stage.get("$set", {}).items():
                    document[key] = _resolve_update_value(document, value)
            return FakeUpdateResult(modified_count=1)
        for key, value in update.get("$set", {}).items():
            document[key] = value
        for key in update.get("$unset", {}):
            document.pop(key, None)
        for key, value in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + value
        return FakeUpdateResult(modified_count=1)

    async def update_many(self, query, update, **kwargs):
        self.calls.append(("update_many", kwargs))
        modified_count = 0
        for document in self.documents.values():
            if _matches_query(document, query):
                for key, value in update.get("$set", {}).items():
                    document[key] = value
                modified_count += 1
        return FakeUpdateResult(modified_count=modified_count)

    async def find_one_and_update(self, query, update, upsert=False, **kwargs):
        self.calls.append(("find_one_and_update", kwargs))
        document = await self.find_one(query)
        if document is None:
            if not upsert:
                return None
            if query.get("lock_key") is not None and any(
                item.get("lock_key") == query["lock_key"]
                for item in self.documents.values()
            ):
                return None
            document_id = update.get("$setOnInsert", {}).get("_id", query.get("_id"))
            document = {"_id": document_id}
            self.documents[document_id] = document
        for key, value in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + value
        for key, value in update.get("$setOnInsert", {}).items():
            document.setdefault(key, value)
        for key, value in update.get("$set", {}).items():
            document[key] = value
        return document


class DuplicateOnReplaceCollection(FakeCollection):
    async def replace_one(self, query, document, upsert=False, **kwargs):
        self.calls.append(("replace_one", kwargs))
        raise DuplicateKeyError("duplicate key")


class FakeUpdateResult:
    def __init__(self, modified_count: int, matched_count: int | None = None) -> None:
        self.modified_count = modified_count
        self.matched_count = modified_count if matched_count is None else matched_count


class FakeSession:
    def __init__(self) -> None:
        self.started = False
        self.committed = False
        self.aborted = False
        self.ended = False

    def start_transaction(self) -> None:
        self.started = True

    async def commit_transaction(self) -> None:
        self.committed = True

    async def abort_transaction(self) -> None:
        self.aborted = True

    async def end_session(self) -> None:
        self.ended = True


class FakeClient:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    async def start_session(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


def _matches_stock_expression(document, query) -> bool:
    if "$expr" not in query:
        return True
    required_quantity = query["$expr"]["$gte"][1]
    available_stock = (
        document["total_stock"] - document["reserved_stock"] - document["sold_stock"]
    )
    return available_stock >= required_quantity


def _matches_query(document, query) -> bool:
    for key, value in query.items():
        if key == "$expr":
            continue
        if key == "$and":
            if not all(_matches_query(document, item) for item in value):
                return False
            continue
        if key == "$or":
            if not any(_matches_query(document, item) for item in value):
                return False
            continue
        document_value = _document_value(document, key)
        if isinstance(value, dict):
            if document_value is None and any(
                operator in value for operator in ("$gte", "$gt", "$lte", "$lt")
            ):
                return False
            if "$in" in value and document_value not in value["$in"]:
                return False
            if "$ne" in value and document_value == value["$ne"]:
                return False
            if "$gte" in value and not document_value >= value["$gte"]:
                return False
            if "$gt" in value and not document_value > value["$gt"]:
                return False
            if "$lt" in value and not document_value < value["$lt"]:
                return False
            if "$lte" in value and not document_value <= value["$lte"]:
                return False
            continue
        if document_value != value:
            return False
    return True


def _document_value(document, key):
    value = document
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _resolve_update_value(document, value):
    if isinstance(value, str) and value.startswith("$"):
        return document.get(value.removeprefix("$"))
    return value


class FakeDatabase:
    def __init__(self) -> None:
        self.client = FakeClient()
        self.admin_accounts = FakeCollection()
        self.admin_auth_tokens = FakeCollection()
        self.products = FakeCollection()
        self.subscription_plans = FakeCollection()
        self.one_time_skus = FakeCollection()
        self.checkouts = FakeCollection()
        self.payment_customers = FakeCollection()
        self.payment_cancel_requests = FakeCollection()
        self.operation_locks = FakeCollection()
        self.operation_lock_counters = FakeCollection()
        self.payments = FakeCollection()
        self.invoices = FakeCollection()
        self.idempotency_keys = FakeCollection()
        self.subscriptions = FakeCollection()
        self.billing_auths = FakeCollection()
        self.billing_methods = FakeCollection()
        self.payment_instruments = FakeCollection()
        self.operator_audits = FakeCollection()
        self.webhook_events = FakeCollection()
        self.notification_outbox = FakeCollection()
        self.notification_templates = FakeCollection()


def motor_collection_stub(
    collection: FakeCollection,
) -> AsyncIOMotorCollection[TestMongoDocument]:
    return cast(AsyncIOMotorCollection[TestMongoDocument], collection)


def motor_database_stub(
    database: FakeDatabase,
) -> AsyncIOMotorDatabase[TestMongoDocument]:
    return cast(AsyncIOMotorDatabase[TestMongoDocument], database)


async def test_mongo_admin_auth_uow_wraps_account_and_token_updates() -> None:
    database = FakeDatabase()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    database.admin_accounts = FakeCollection(
        [
            {
                "_id": "admin_1",
                "email": "ops@example.com",
                "email_lower": "ops@example.com",
                "password_hash": "hash-old",
                "display_name": "운영 담당자",
                "status": "active",
                "roles": ["operator"],
                "permissions": ["payment_read"],
                "permission_version": 1,
                "failed_login_count": 0,
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    database.admin_auth_tokens = FakeCollection(
        [
            {
                "_id": "aatok_reset",
                "admin_account_id": "admin_1",
                "token_type": "password_reset",
                "token_hash": "reset_hash",
                "status": "active",
                "expires_at": now,
                "created_at": now,
            },
            {
                "_id": "aatok_refresh",
                "admin_account_id": "admin_1",
                "token_type": "refresh_token",
                "token_hash": "refresh_hash",
                "status": "active",
                "expires_at": now,
                "created_at": now,
            },
        ]
    )

    async with MongoAdminAuthUnitOfWorkFactory(motor_database_stub(database))() as uow:
        admin = await uow.admin_auth.get_admin_account("admin_1")
        token = await uow.admin_auth.get_auth_token_by_hash("reset_hash")
        assert isinstance(admin, AdminAccount)
        assert isinstance(token, AdminAuthToken)
        admin.password_hash = "hash-new"
        admin.updated_at = now
        token.status = "consumed"
        token.consumed_at = now
        await uow.admin_auth.save_admin_account(admin)
        await uow.admin_auth.save_auth_token(token)
        await uow.admin_auth.revoke_active_refresh_tokens(
            "admin_1",
            now,
            request_ip="203.0.113.50",
            user_agent="admin-console/logout",
        )

    [session] = database.client.sessions
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.admin_accounts.documents["admin_1"]["password_hash"] == "hash-new"
    assert database.admin_auth_tokens.documents["aatok_reset"]["status"] == "consumed"
    assert database.admin_auth_tokens.documents["aatok_refresh"]["status"] == "revoked"
    assert database.admin_auth_tokens.documents["aatok_refresh"]["last_used_at"] == now
    assert database.admin_auth_tokens.documents["aatok_refresh"]["request_ip"] == (
        "203.0.113.50"
    )
    assert database.admin_auth_tokens.documents["aatok_refresh"]["user_agent"] == (
        "admin-console/logout"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.admin_accounts.calls
        if method in {"find_one", "replace_one"}
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.admin_auth_tokens.calls
        if method in {"find_one", "replace_one", "update_many"}
    )


async def test_mongo_admin_subscription_adjust_uow_wraps_adjustment_writes() -> None:
    database = FakeDatabase()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    subscription = Subscription(
        id="sub_adjust",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        next_billing_at=now,
        current_period_start_at=now,
        current_period_end_at=now,
        cancel_at_period_end=False,
    )
    payment = Payment(
        id="pay_adjust",
        order_id="order_adjust",
        amount=9900,
        status="paid",
        created_at=now,
        subscription_id=subscription.id,
        payment_key="paykey_adjust",
        approved_at=now,
    )
    invoice = Invoice(
        id="inv_adjust",
        user_id="user_1",
        payment_id=payment.id,
        subscription_id=subscription.id,
        status="paid",
        issued_at=now,
    )
    key = IdempotencyKey(
        id="idem_adjust",
        scope="admin-subscription-adjust",
        key_hash="key_hash",
        request_hash="request_hash",
        status="succeeded",
        created_at=now,
        updated_at=now,
        expires_at=now,
        resource_type="subscription_adjustment",
        resource_id="audit_adjust",
        response_status=200,
        response_body={"subscriptionId": subscription.id},
    )

    async with MongoAdminSubscriptionAdjustUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        await uow.admin_operations.save_admin_subscription(subscription)
        await uow.admin_operations.save_admin_payment(payment)
        await uow.admin_operations.save_admin_invoice(invoice)
        await uow.admin_operations.save_subscription_adjustment_audit_record(
            audit_id="audit_adjust",
            subscription_id=subscription.id,
            admin_id="admin_1",
            request_id="req_adjust",
            adjustment_type="provider_payment_sync",
            reason_code="webhook_recovery",
            reason_message="provider DONE sync",
            previous={"status": "past_due"},
            next_value={"status": "active"},
            notified_customer=False,
            idempotency_key_id=key.id,
            idempotency_scope=key.scope,
            idempotency_key_hash=key.key_hash,
            idempotency_request_hash=key.request_hash,
        )
        await uow.idempotency_keys.save_idempotency_key(key)

    [session] = database.client.sessions
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.subscriptions.documents[subscription.id]["status"] == "active"
    assert database.payments.documents[payment.id]["status"] == "paid"
    assert database.invoices.documents[invoice.id]["status"] == "paid"
    assert database.operator_audits.documents["audit_adjust"]["target_id"] == (
        subscription.id
    )
    assert database.idempotency_keys.documents[key.id]["status"] == "succeeded"
    for collection in (
        database.subscriptions,
        database.payments,
        database.invoices,
        database.operator_audits,
        database.idempotency_keys,
    ):
        assert any(kwargs.get("session") is session for _, kwargs in collection.calls)


async def test_ensure_mongo_indexes_requests_first_slice_indexes() -> None:
    database = FakeDatabase()

    await ensure_mongo_indexes(motor_database_stub(database))

    assert any(
        index[1]["name"] == "uniq_products_code_type"
        for index in database.products.indexes
    )
    assert any(
        index[1]["name"] == "idx_payments_checkout_id"
        for index in database.payments.indexes
    )
    assert any(
        index[1]["name"] == "uniq_notification_outbox_idempotency_key"
        for index in database.notification_outbox.indexes
    )
    assert any(
        index[1]["name"] == "ttl_notification_outbox_purge_after"
        and index[1]["expireAfterSeconds"] == 0
        for index in database.notification_outbox.indexes
    )
    assert any(
        index[1]["name"] == "uniq_notification_templates_key_version"
        for index in database.notification_templates.indexes
    )
    assert any(
        index[1]["name"] == "idx_payments_ready_expires_at"
        and index[1]["partialFilterExpression"]
        == {"status": "ready", "expires_at": {"$type": "date"}}
        for index in database.payments.indexes
    )
    assert any(
        index[1]["name"] == "uniq_payments_payment_key_sparse"
        and index[1]["unique"] is True
        and index[1]["sparse"] is True
        for index in database.payments.indexes
    )
    assert any(
        index[1]["name"] == "uniq_payments_paid_checkout"
        and index[1]["unique"] is True
        and index[1]["partialFilterExpression"]
        == {"checkout_id": {"$type": "string"}, "status": "paid"}
        for index in database.payments.indexes
    )
    assert any(
        index[1]["name"] == "idx_one_time_skus_product_status_stock_policy"
        for index in database.one_time_skus.indexes
    )
    assert any(
        index[1]["name"] == "uniq_payment_customers_user_provider"
        for index in database.payment_customers.indexes
    )
    assert any(
        index[1]["name"] == "uniq_payment_customers_provider_customer_key"
        for index in database.payment_customers.indexes
    )
    assert any(
        index[1]["name"] == "uniq_payment_cancel_requests_idempotency"
        for index in database.payment_cancel_requests.indexes
    )
    assert any(
        index[1]["name"] == "idx_payment_cancel_requests_pending_created_at"
        and index[1]["partialFilterExpression"] == {"status": "pending"}
        for index in database.payment_cancel_requests.indexes
    )
    assert any(
        index[1]["name"] == "idx_billing_auths_user_status"
        and index[0] == [("user_id", 1), ("status", 1)]
        for index in database.billing_auths.indexes
    )
    assert any(
        index[1]["name"] == "uniq_operation_locks_lock_key"
        and index[1]["unique"] is True
        for index in database.operation_locks.indexes
    )
    assert any(
        index[1]["name"] == "idx_operation_locks_status_until"
        for index in database.operation_locks.indexes
    )
    assert any(
        index[1]["name"] == "ttl_operation_locks_locked_until_at"
        and index[1]["expireAfterSeconds"] == 0
        for index in database.operation_locks.indexes
    )
    assert any(
        index[1]["name"] == "idx_operator_audits_target"
        for index in database.operator_audits.indexes
    )
    assert any(
        index[1]["name"] == "idx_operator_audits_operator"
        for index in database.operator_audits.indexes
    )
    assert any(
        index[1]["name"] == "idx_operator_audits_action"
        for index in database.operator_audits.indexes
    )
    assert any(
        index[1]["name"] == "idx_payments_failed_retry_scheduled_at"
        and index[1]["partialFilterExpression"]
        == {"status": "failed", "retry_scheduled_at": {"$type": "date"}}
        for index in database.payments.indexes
    )
    assert any(
        index[1]["name"] == "uniq_payments_subscription_billing_cycle_paid"
        and index[1]["unique"] is True
        and index[1]["partialFilterExpression"]
        == {
            "subscription_id": {"$type": "string"},
            "billing_cycle_key": {"$type": "string"},
            "status": "paid",
        }
        for index in database.payments.indexes
    )
    assert any(
        index[1]["name"] == "uniq_invoices_subscription_billing_cycle"
        and index[1]["unique"] is True
        and index[1]["partialFilterExpression"]
        == {
            "subscription_id": {"$type": "string"},
            "billing_cycle_key": {"$type": "string"},
            "status": {"$in": ["issued", "paid"]},
        }
        for index in database.invoices.indexes
    )
    assert any(
        index[1]["name"] == "uniq_idempotency_keys_scope_key"
        and index[1]["unique"] is True
        for index in database.idempotency_keys.indexes
    )
    assert any(
        index[1]["name"] == "idx_idempotency_keys_resource"
        for index in database.idempotency_keys.indexes
    )
    assert any(
        index[1]["name"] == "ttl_idempotency_keys_expires_at"
        for index in database.idempotency_keys.indexes
    )
    assert any(
        index[1]["name"] == "idx_subscriptions_user_status"
        for index in database.subscriptions.indexes
    )
    assert any(
        index[1]["name"] == "idx_subscriptions_next_billing_status"
        for index in database.subscriptions.indexes
    )
    assert any(
        index[1]["name"] == "uniq_subscriptions_user_product_service_holding"
        and index[1]["unique"] is True
        and index[1]["partialFilterExpression"]
        == {
            "status": {
                "$in": ["pending", "active", "past_due", "cancel_scheduled"]
            }
        }
        for index in database.subscriptions.indexes
    )
    assert any(
        index[1]["name"] == "uniq_billing_methods_active_default"
        and index[1]["unique"] is True
        and index[1]["partialFilterExpression"]
        == {"is_default": True, "status": "active"}
        for index in database.billing_methods.indexes
    )
    assert any(
        index[1]["name"] == "uniq_payment_instruments_provider_billing_key_hash"
        and index[1]["unique"] is True
        for index in database.payment_instruments.indexes
    )
    assert any(
        index[1]["name"] == "uniq_webhook_events_provider_event"
        for index in database.webhook_events.indexes
    )


async def test_mongo_notification_template_repository_resolves_fallback_order() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    repository = MongoNotificationTemplateRepository(
        motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "ntpl_default",
                        "template_key": "default.subscription_payment_failed",
                        "version": 1,
                        "event_type": "subscription_payment_failed",
                        "status": "active",
                        "subject_template": "default",
                        "html_template": "{{ invoiceId }}",
                        "text_template": "{{ invoiceId }}",
                        "required_template_args": ["invoiceId"],
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "_id": "ntpl_type",
                        "template_key": "subscription.subscription_payment_failed",
                        "version": 1,
                        "event_type": "subscription_payment_failed",
                        "product_type": "subscription",
                        "status": "active",
                        "subject_template": "type",
                        "html_template": "{{ invoiceId }}",
                        "text_template": "{{ invoiceId }}",
                        "required_template_args": ["invoiceId"],
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "_id": "ntpl_product",
                        "template_key": "basic.subscription_payment_failed",
                        "version": 1,
                        "event_type": "subscription_payment_failed",
                        "product_code": "basic",
                        "status": "active",
                        "subject_template": "product",
                        "html_template": "{{ invoiceId }}",
                        "text_template": "{{ invoiceId }}",
                        "required_template_args": ["invoiceId"],
                        "created_at": now,
                        "updated_at": now,
                    },
                ]
            )
        )
    )

    template = await repository.resolve_active_template(
        event_type="subscription_payment_failed",
        product_code="basic",
        product_type="subscription",
    )

    assert template is not None
    assert template.template_key == "basic.subscription_payment_failed"


async def test_mongo_notification_outbox_claim_respects_locked_until() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    collection = FakeCollection(
        [
            {
                "_id": "nout_due",
                "idempotency_key": "email:one_time_payment_paid:chk_1:pay_1",
                "idempotency_payload_hash": "hash",
                "event_type": "one_time_payment_paid",
                "recipient_type": "user",
                "recipient_user_id": "user_1",
                "recipient_email": "user@example.com",
                "template_key": "default.one_time_payment_paid",
                "template_version": 1,
                "template_args": {"checkoutId": "chk_1"},
                "status": "pending",
                "attempt_count": 0,
                "available_at": now,
                "created_at": now,
                "updated_at": now,
            },
            {
                "_id": "nout_locked",
                "idempotency_key": "email:one_time_payment_paid:chk_2:pay_2",
                "idempotency_payload_hash": "hash",
                "event_type": "one_time_payment_paid",
                "recipient_type": "user",
                "recipient_user_id": "user_2",
                "recipient_email": "user2@example.com",
                "template_key": "default.one_time_payment_paid",
                "template_version": 1,
                "template_args": {"checkoutId": "chk_2"},
                "status": "retry_scheduled",
                "attempt_count": 1,
                "available_at": now,
                "locked_until_at": now + timedelta(minutes=1),
                "created_at": now,
                "updated_at": now,
            },
        ]
    )
    repository = MongoNotificationOutboxRepository(motor_collection_stub(collection))

    claimed = await repository.claim_due_notifications(
        now=now,
        lock_until=now + timedelta(minutes=5),
        worker_id="worker-1",
        limit=100,
    )

    assert [item.id for item in claimed] == ["nout_due"]
    assert collection.documents["nout_due"]["status"] == "processing"
    assert collection.documents["nout_due"]["worker_id"] == "worker-1"
    assert collection.documents["nout_due"]["attempt_count"] == 1
    assert collection.documents["nout_locked"]["status"] == "retry_scheduled"


async def test_mongo_notification_outbox_updates_retry_and_dead_letter() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    collection = FakeCollection(
        [
            {
                "_id": "nout_1",
                "idempotency_key": "email:admin_auth.login_link:aatok_1",
                "idempotency_payload_hash": "hash",
                "event_type": "admin_auth.login_link",
                "recipient_type": "admin",
                "recipient_admin_id": "admin_1",
                "recipient_email": "ops@example.com",
                "template_key": "default.admin_auth.login_link",
                "template_version": 1,
                "template_args": {"expiresMinutes": 10},
                "status": "processing",
                "attempt_count": 1,
                "available_at": now,
                "locked_until_at": now + timedelta(minutes=5),
                "worker_id": "worker-1",
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    repository = MongoNotificationOutboxRepository(motor_collection_stub(collection))
    error = NotificationLastError(
        code="smtp_timeout",
        message="SMTP timeout",
        retryable=True,
        occurred_at=now,
    )

    await repository.schedule_retry(
        "nout_1",
        available_at=now + timedelta(minutes=1),
        last_error=error,
    )
    await repository.mark_dead_letter(
        "nout_1",
        last_error=NotificationLastError(
            code="template_render_failed",
            message="template render failed",
            retryable=False,
            occurred_at=now + timedelta(seconds=1),
        ),
        purge_after_at=now + timedelta(days=180),
    )

    document = collection.documents["nout_1"]
    assert document["status"] == "dead_letter"
    assert document["last_error"]["code"] == "template_render_failed"
    assert document["purge_after_at"] == now + timedelta(days=180)
    assert "locked_until_at" not in document


async def test_mongo_operation_lock_repository_acquires_and_releases_lock() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    operation_locks = FakeCollection()
    counters = FakeCollection()
    repository = MongoOperationLockRepository(
        operation_locks=motor_collection_stub(operation_locks),
        operation_lock_counters=motor_collection_stub(counters),
    )

    first = await repository.acquire_operation_lock(
        lock_key="subscription:sub_1",
        owner_token="owner_1",
        fencing_counter_key="subscription",
        acquired_at=now,
        locked_until_at=datetime(2026, 6, 10, 0, 5, tzinfo=UTC),
        metadata={"api": "subscriptions-change"},
    )
    blocked = await repository.acquire_operation_lock(
        lock_key="subscription:sub_1",
        owner_token="owner_2",
        fencing_counter_key="subscription",
        acquired_at=now,
        locked_until_at=datetime(2026, 6, 10, 0, 5, tzinfo=UTC),
    )
    await repository.release_operation_lock(
        lock_key="subscription:sub_1",
        owner_token="owner_1",
        released_at=datetime(2026, 6, 10, 0, 1, tzinfo=UTC),
    )
    second = await repository.acquire_operation_lock(
        lock_key="subscription:sub_1",
        owner_token="owner_2",
        fencing_counter_key="subscription",
        acquired_at=datetime(2026, 6, 10, 0, 2, tzinfo=UTC),
        locked_until_at=datetime(2026, 6, 10, 0, 7, tzinfo=UTC),
    )

    assert first is not None
    assert first.fencing_token == 1
    assert blocked is None
    assert second is not None
    assert second.id == first.id
    assert second.fencing_token == 3
    assert operation_locks.documents[first.id]["status"] == "active"
    assert counters.documents["subscription"]["seq"] == 3


async def test_mongo_webhook_repository_updates_invoice_and_subscription() -> None:
    invoices = FakeCollection()
    subscriptions = FakeCollection()
    repository = MongoWebhookRepository(
        webhook_events=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(invoices),
        subscriptions=motor_collection_stub(subscriptions),
    )

    await repository.save_invoice(
        Invoice(
            id="inv_1",
            user_id="user_1",
            payment_id="pay_1",
            status="paid",
            issued_at=datetime(2026, 6, 10, tzinfo=UTC),
            subscription_id="sub_1",
            receipt_url="https://example.com/receipt",
        )
    )
    await repository.save_subscription(
        Subscription(
            id="sub_1",
            user_id="user_1",
            payment_customer_id="pcus_1",
            plan_id="plan_basic_monthly",
            product_code="basic",
            status="active",
            cancel_at_period_end=False,
        )
    )

    saved_invoice = await repository.get_invoice_by_payment_id("pay_1")
    saved_subscription = await repository.get_subscription("sub_1")

    assert saved_invoice is not None
    assert saved_invoice.id == "inv_1"
    assert saved_subscription is not None
    assert saved_subscription.status == "active"
    assert invoices.documents["inv_1"]["receipt_url"] == "https://example.com/receipt"
    assert subscriptions.documents["sub_1"]["status"] == "active"


async def test_mongo_subscription_checkout_repository_gets_open_cycle_invoice() -> (
    None
):
    invoices = FakeCollection(
        [
            {
                "_id": "inv_open",
                "user_id": "user_1",
                "payment_id": "pay_1",
                "status": "issued",
                "issued_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_1",
                "billing_cycle_key": "sub_1:2026-06-10T00:00:00+00:00",
            },
            {
                "_id": "inv_closed",
                "user_id": "user_1",
                "payment_id": "pay_2",
                "status": "voided",
                "issued_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_1",
                "billing_cycle_key": "sub_1:2026-06-10T00:00:00+00:00",
            },
        ]
    )
    repository = MongoSubscriptionCheckoutRepository(
        subscriptions=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(invoices),
    )

    invoice = await repository.get_open_invoice_for_subscription_cycle(
        "sub_1",
        "sub_1:2026-06-10T00:00:00+00:00",
    )

    assert invoice is not None
    assert invoice.id == "inv_open"


async def test_mongo_subscription_checkout_repository_omits_empty_invoice_fields() -> (
    None
):
    now = datetime(2026, 6, 10, tzinfo=UTC)
    invoices = FakeCollection()
    repository = MongoSubscriptionCheckoutRepository(
        subscriptions=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(invoices),
    )

    await repository.save_invoice(
        Invoice(
            id="inv_1",
            user_id="user_1",
            payment_id="pay_1",
            status="issued",
            issued_at=now,
            subscription_id="sub_1",
            billing_cycle_key="cycle_1",
        )
    )

    assert invoices.documents["inv_1"] == {
        "_id": "inv_1",
        "user_id": "user_1",
        "payment_id": "pay_1",
        "status": "issued",
        "issued_at": now,
        "subscription_id": "sub_1",
        "billing_cycle_key": "cycle_1",
    }


async def test_mongo_subscription_checkout_repository_translates_duplicate_subscription(
) -> None:
    repository = MongoSubscriptionCheckoutRepository(
        subscriptions=motor_collection_stub(DuplicateOnReplaceCollection()),
        payments=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(InvalidStateTransitionError, match="active subscription"):
        await repository.save_subscription(
            Subscription(
                id="sub_1",
                user_id="user_1",
                payment_customer_id="pcus_1",
                plan_id="plan_basic_monthly",
                product_code="basic",
                status="pending",
                cancel_at_period_end=False,
            )
        )


async def test_mongo_webhook_repository_finds_processed_payment_status_event() -> None:
    webhook_events = FakeCollection(
        [
            {
                "_id": "wh_1",
                "provider": "tosspayments",
                "event_id": "evt_done",
                "status": "processed",
                "payload": {
                    "eventType": "PAYMENT_STATUS_CHANGED",
                    "status": "DONE",
                    "paymentKey": "paykey_123",
                    "orderId": "ord_123",
                },
            },
            {
                "_id": "wh_2",
                "provider": "tosspayments",
                "event_id": "evt_pending",
                "status": "received",
                "payload": {"status": "DONE", "paymentKey": "paykey_123"},
            },
        ]
    )
    repository = MongoWebhookRepository(
        webhook_events=motor_collection_stub(webhook_events),
        payments=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
    )

    event = await repository.get_processed_webhook_event_by_payment_status(
        provider="tosspayments",
        payment_key="paykey_123",
        provider_status="DONE",
        exclude_event_id="evt_new",
    )
    excluded = await repository.get_processed_webhook_event_by_payment_status(
        provider="tosspayments",
        payment_key="paykey_123",
        provider_status="DONE",
        exclude_event_id="evt_done",
    )

    assert event == WebhookEvent(
        id="wh_1",
        provider="tosspayments",
        event_id="evt_done",
        event_type="PAYMENT_STATUS_CHANGED",
        payment_key="paykey_123",
        order_id="ord_123",
        status="processed",
        payload={
            "eventType": "PAYMENT_STATUS_CHANGED",
            "status": "DONE",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
        },
    )
    assert excluded is None


async def test_mongo_webhook_repository_picks_latest_payment_status_event() -> None:
    webhook_events = FakeCollection(
        [
            {
                "_id": "wh_old",
                "provider": "tosspayments",
                "event_id": "evt_old",
                "status": "ignored",
                "payload": {
                    "eventType": "PAYMENT_STATUS_CHANGED",
                    "status": "CANCELED",
                    "paymentKey": "paykey_123",
                    "orderId": "ord_123",
                    "statusChangedAt": "2026-06-10T00:09:59+00:00",
                },
            },
            {
                "_id": "wh_new",
                "provider": "tosspayments",
                "event_id": "evt_new",
                "status": "processed",
                "payload": {
                    "eventType": "PAYMENT_STATUS_CHANGED",
                    "status": "CANCELED",
                    "paymentKey": "paykey_123",
                    "orderId": "ord_123",
                    "statusChangedAt": "2026-06-10T00:12:00+00:00",
                },
            },
        ]
    )
    repository = MongoWebhookRepository(
        webhook_events=motor_collection_stub(webhook_events),
        payments=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
    )

    event = await repository.get_processed_webhook_event_by_payment_status(
        provider="tosspayments",
        payment_key="paykey_123",
        provider_status="CANCELED",
        exclude_event_id="evt_retry",
    )

    assert event is not None
    assert event.event_id == "evt_new"


async def test_mongo_webhook_repository_saves_documented_event_fields_only() -> None:
    webhook_events = FakeCollection()
    repository = MongoWebhookRepository(
        webhook_events=motor_collection_stub(webhook_events),
        payments=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
    )

    await repository.save_webhook_event(
        WebhookEvent(
            id="wh_documented",
            provider="tosspayments",
            event_id="evt_documented",
            event_type="PAYMENT_STATUS_CHANGED",
            payment_key="paykey_documented",
            order_id="ord_documented",
            status="processed",
            payload={
                "eventType": "PAYMENT_STATUS_CHANGED",
                "paymentKey": "paykey_documented",
                "orderId": "ord_documented",
                "status": "DONE",
            },
            received_at=datetime(2026, 6, 10, tzinfo=UTC),
            processed_at=datetime(2026, 6, 10, tzinfo=UTC),
        )
    )

    assert webhook_events.documents["wh_documented"] == {
        "_id": "wh_documented",
        "provider": "tosspayments",
        "event_id": "evt_documented",
        "status": "processed",
        "payload": {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "paymentKey": "paykey_documented",
            "orderId": "ord_documented",
            "status": "DONE",
        },
    }


async def test_mongo_admin_catalog_repository_loads_product_by_code_and_type() -> None:
    repository = MongoAdminCatalogRepository(
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_subscription",
                        "product_code": "ANALYTICS",
                        "product_type": "subscription",
                        "name": "Analytics subscription",
                        "status": "draft",
                    },
                    {
                        "_id": "product_one_time",
                        "product_code": "ANALYTICS",
                        "product_type": "one_time",
                        "name": "Analytics reports",
                        "status": "draft",
                    },
                ]
            )
        ),
        operator_audits=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
    )

    product = await repository.get_product_by_code("ANALYTICS", "one_time")

    assert product is not None
    assert product.id == "product_one_time"
    assert product.product_type == "one_time"


async def test_mongo_admin_catalog_repository_saves_operator_audit_document() -> None:
    operator_audits = FakeCollection()
    created_at = datetime(2026, 6, 10, tzinfo=UTC)
    repository = MongoAdminCatalogRepository(
        products=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(operator_audits),
        subscription_plans=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
    )

    await repository.save_product_audit_record(
        product_id="product_123",
        admin_id="admin_1",
        request_id="req_1",
        action="product.create",
        previous=None,
        next_value={
            "product_id": "product_123",
            "product_code": "analytics",
            "status": "draft",
        },
        request_ip="203.0.113.30",
        created_at=created_at,
    )

    document = operator_audits.documents["req_1:product.create:product_123"]
    assert document["operator_id"] == "admin_1"
    assert document["action"] == "product.create"
    assert document["target_type"] == "product"
    assert document["target_id"] == "product_123"
    assert document["previous_state"] == {}
    assert document["next_state"]["product_code"] == "analytics"
    assert document["reason_code"] == "product.create"
    assert document["request_ip"] == "203.0.113.30"
    assert document["result"] == "succeeded"
    assert document["created_at"] == created_at

    await repository.save_product_audit_record(
        product_id="product_456",
        admin_id="admin_1",
        request_id="req_2",
        action="product.create",
        previous=None,
        next_value={
            "product_id": "product_456",
            "product_code": "reports",
            "status": "draft",
        },
    )

    document_without_ip = operator_audits.documents["req_2:product.create:product_456"]
    assert "request_ip" not in document_without_ip


async def test_mongo_admin_catalog_repository_saves_audit_reason_message() -> None:
    operator_audits = FakeCollection()
    repository = MongoAdminCatalogRepository(
        products=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(operator_audits),
        subscription_plans=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
    )

    await repository.save_product_audit_record(
        product_id="product_123",
        admin_id="admin_1",
        request_id="req_1",
        action="subscription_plan.update",
        previous={"amount": 9900},
        next_value={
            "plan_id": "plan_basic",
            "product_id": "product_123",
            "amount": 12900,
            "change_reason": "좌석 한도와 신규 가입 가격 조정",
        },
        request_ip="203.0.113.30",
    )

    document = operator_audits.documents[
        "req_1:subscription_plan.update:product_123"
    ]
    assert document["target_type"] == "subscription_plan"
    assert document["target_id"] == "plan_basic"
    assert document["reason_message"] == "좌석 한도와 신규 가입 가격 조정"


async def test_mongo_admin_catalog_repository_translates_product_duplicate_key(
) -> None:
    repository = MongoAdminCatalogRepository(
        products=motor_collection_stub(DuplicateOnReplaceCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(InvalidStateTransitionError, match="product code"):
        await repository.save_product(
            Product(
                id="product_reports",
                product_code="REPORTS",
                product_type="one_time",
                name="Reports",
                status="draft",
            )
        )


async def test_mongo_admin_catalog_repository_translates_plan_duplicate_key() -> None:
    repository = MongoAdminCatalogRepository(
        products=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(DuplicateOnReplaceCollection()),
        one_time_skus=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(InvalidStateTransitionError, match="subscription plan code"):
        await repository.save_subscription_plan(
            SubscriptionPlan(
                id="plan_basic",
                product_id="product_analytics",
                plan_code="ANALYTICS_BASIC_MONTHLY",
                billing_period="monthly",
                amount=9900,
                entitlements={},
                status="active",
            )
        )


async def test_mongo_admin_catalog_repository_translates_sku_duplicate_key() -> None:
    repository = MongoAdminCatalogRepository(
        products=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(DuplicateOnReplaceCollection()),
    )

    with pytest.raises(InvalidStateTransitionError, match="one-time sku code"):
        await repository.save_one_time_sku(
            OneTimeSku(
                id="sku_report_pack",
                product_id="product_reports",
                sku_code="REPORT_PACK_100",
                amount=50000,
                stock_policy="unlimited",
                status="active",
            )
        )


async def test_mongo_admin_catalog_repository_saves_fixed_currency_as_default() -> None:
    subscription_plans = FakeCollection()
    one_time_skus = FakeCollection()
    repository = MongoAdminCatalogRepository(
        products=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(subscription_plans),
        one_time_skus=motor_collection_stub(one_time_skus),
    )

    await repository.save_subscription_plan(
        SubscriptionPlan(
            id="plan_basic",
            product_id="product_analytics",
            plan_code="ANALYTICS_BASIC_MONTHLY",
            billing_period="monthly",
            amount=9900,
            entitlements={},
            status="active",
            currency="KRW",
        )
    )
    await repository.save_one_time_sku(
        OneTimeSku(
            id="sku_report_pack",
            product_id="product_reports",
            sku_code="REPORT_PACK_100",
            amount=50000,
            stock_policy="unlimited",
            status="active",
            currency="KRW",
        )
    )

    assert "currency" not in subscription_plans.documents["plan_basic"]
    assert "currency" not in one_time_skus.documents["sku_report_pack"]
    assert one_time_skus.documents["sku_report_pack"] == {
        "_id": "sku_report_pack",
        "product_id": "product_reports",
        "sku_code": "REPORT_PACK_100",
        "amount": 50000,
        "stock_policy": "unlimited",
        "status": "active",
    }
    loaded_plan = await repository.get_subscription_plan(
        "product_analytics",
        "plan_basic",
    )
    loaded_sku = await repository.get_one_time_sku(
        "product_reports",
        "sku_report_pack",
    )
    assert loaded_plan is not None
    assert loaded_plan.currency == "KRW"
    assert loaded_sku is not None
    assert loaded_sku.currency == "KRW"


async def test_mongo_admin_operations_repository_saves_operator_audit() -> None:
    operator_audits = FakeCollection()
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(operator_audits),
    )

    await repository.save_subscription_adjustment_audit_record(
        audit_id="audit_1",
        subscription_id="sub_1",
        admin_id="admin_1",
        request_id="req_1",
        adjustment_type="postpone_next_billing",
        reason_code="service_incident",
        reason_message="incident compensation",
        previous={"status": "active"},
        next_value={"status": "active", "nextBillingAt": "2026-07-08"},
        notified_customer=True,
        request_ip="203.0.113.20",
        idempotency_key_id="idem_1",
        idempotency_scope="admin-subscription-adjust",
        idempotency_key_hash="key_hash",
        idempotency_request_hash="request_hash",
    )

    document = operator_audits.documents["audit_1"]
    assert document["operator_id"] == "admin_1"
    assert document["action"] == "subscription.adjust"
    assert document["target_type"] == "subscription"
    assert document["target_id"] == "sub_1"
    assert document["idempotency_key_id"] == "idem_1"
    assert document["idempotency_scope"] == "admin-subscription-adjust"
    assert document["idempotency_key_hash"] == "key_hash"
    assert document["idempotency_request_hash"] == "request_hash"
    assert document["request_ip"] == "203.0.113.20"
    assert document["previous_state"] == {"status": "active"}
    assert document["next_state"]["adjustment_type"] == "postpone_next_billing"
    assert document["reason_code"] == "service_incident"
    assert document["reason_message"] == "incident compensation"
    assert document["result"] == "succeeded"


async def test_mongo_admin_operations_repository_saves_list_access_audit() -> None:
    operator_audits = FakeCollection()
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(operator_audits),
    )
    created_at = datetime(2026, 6, 10, tzinfo=UTC)

    await repository.save_admin_list_audit_record(
        audit_id="audit_list_1",
        admin_id="admin_1",
        request_id="req_list_1",
        action="payment.list",
        target_type="payment",
        target_id="admin-payments",
        query={"status": ["paid"], "paymentKey": "paykey_123", "limit": 50},
        result_count=1,
        has_more=False,
        request_ip="203.0.113.30",
        created_at=created_at,
    )

    document = operator_audits.documents["audit_list_1"]
    assert document["operator_id"] == "admin_1"
    assert document["action"] == "payment.list"
    assert document["target_type"] == "payment"
    assert document["target_id"] == "admin-payments"
    assert document["request_id"] == "req_list_1"
    assert document["request_ip"] == "203.0.113.30"
    assert document["previous_state"] == {}
    assert document["next_state"] == {
        "query": {"status": ["paid"], "paymentKey": "paykey_123", "limit": 50},
        "result_count": 1,
        "has_more": False,
    }
    assert document["reason_code"] == "admin_list_query"
    assert document["result"] == "succeeded"
    assert document["created_at"] == created_at


async def test_mongo_admin_operations_repository_filters_payments_by_joined_user() -> (
    None
):
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_other",
                        "checkout_id": "chk_other",
                        "order_id": "order_other",
                        "amount": 1000,
                        "status": "paid",
                        "created_at": datetime(2026, 6, 9, tzinfo=UTC),
                        "approved_at": datetime(2026, 6, 9, tzinfo=UTC),
                        "payment_key": "paykey_other",
                    },
                    {
                        "_id": "pay_target",
                        "checkout_id": "chk_target",
                        "order_id": "order_target",
                        "amount": 2000,
                        "status": "paid",
                        "created_at": datetime(2026, 6, 8, tzinfo=UTC),
                        "approved_at": datetime(2026, 6, 8, tzinfo=UTC),
                        "payment_key": "paykey_target",
                    },
                ]
            )
        ),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(
            FakeCollection(
                [
                        {
                            "_id": "chk_other",
                            "user_id": "user_2",
                            "payment_customer_id": "customer_2",
                            "items": [],
                            "status": "paid",
                            "created_at": datetime(2026, 6, 9, tzinfo=UTC),
                        },
                        {
                            "_id": "chk_target",
                            "user_id": "user_1",
                            "payment_customer_id": "customer_1",
                            "items": [],
                            "status": "paid",
                            "created_at": datetime(2026, 6, 8, tzinfo=UTC),
                        },
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_admin_payments(
        AdminListQuery(user_id="user_1", limit=1)
    )

    assert [row.payment_id for row in rows] == ["pay_target"]


async def test_mongo_admin_operations_repository_filters_payments_by_created_date() -> (
    None
):
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_failed",
                        "checkout_id": "chk_failed",
                        "order_id": "order_failed",
                        "amount": 1000,
                        "status": "failed",
                        "created_at": datetime(2026, 6, 9, tzinfo=UTC),
                    },
                    {
                        "_id": "pay_old",
                        "checkout_id": "chk_old",
                        "order_id": "order_old",
                        "amount": 1000,
                        "status": "failed",
                        "created_at": datetime(2026, 5, 1, tzinfo=UTC),
                    },
                ]
            )
        ),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_admin_payments(
        AdminListQuery(
            status=("failed",),
            from_at=datetime(2026, 6, 1, tzinfo=UTC),
            to_at=datetime(2026, 6, 30, tzinfo=UTC),
            limit=50,
        )
    )

    assert [row.payment_id for row in rows] == ["pay_failed"]


async def test_mongo_admin_operations_repository_pages_unapproved_payments() -> None:
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_paid",
                        "checkout_id": "chk_paid",
                        "order_id": "order_paid",
                        "amount": 1000,
                        "status": "paid",
                        "created_at": datetime(2026, 6, 8, tzinfo=UTC),
                        "approved_at": datetime(2026, 6, 8, tzinfo=UTC),
                        "payment_key": "paykey_paid",
                    },
                    {
                        "_id": "pay_ready",
                        "checkout_id": "chk_ready",
                        "order_id": "order_ready",
                        "amount": 1000,
                        "status": "ready",
                        "created_at": datetime(2026, 6, 9, tzinfo=UTC),
                    },
                ]
            )
        ),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    first_page = await repository.list_admin_payments(AdminListQuery(limit=1))
    second_page = await repository.list_admin_payments(
        AdminListQuery(
            cursor=encode_cursor(
                {
                    "sortAt": datetime(2026, 6, 9, tzinfo=UTC),
                    "paymentId": "pay_ready",
                }
            ),
            limit=1,
        )
    )

    assert [row.payment_id for row in first_page] == ["pay_ready"]
    assert [row.payment_id for row in second_page] == ["pay_paid"]


async def test_mongo_admin_operations_repository_summarizes_card_last_four() -> None:
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_card",
                        "checkout_id": "chk_card",
                        "order_id": "order_card",
                        "amount": 25000,
                        "status": "paid",
                        "created_at": datetime(2026, 6, 9, tzinfo=UTC),
                        "approved_at": datetime(2026, 6, 9, tzinfo=UTC),
                        "payment_key": "paykey_card",
                        "method": "카드",
                        "method_detail": {
                            "type": "card",
                            "maskedCardNumber": "**** **** **** 4242",
                        },
                    },
                ]
            )
        ),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_admin_payments(AdminListQuery(limit=50))

    assert rows[0].method_summary == "카드 4242"


async def test_mongo_admin_operations_repository_pages_null_next_billing() -> None:
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_canceled_2",
                "user_id": "user_2",
                "payment_customer_id": "customer_2",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "canceled",
                "cancel_at_period_end": False,
                "next_billing_at": None,
            },
            {
                "_id": "sub_active",
                "user_id": "user_1",
                "payment_customer_id": "customer_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
                "next_billing_at": datetime(2026, 7, 1, tzinfo=UTC),
            },
            {
                "_id": "sub_canceled_1",
                "user_id": "user_1",
                "payment_customer_id": "customer_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "canceled",
                "cancel_at_period_end": False,
                "next_billing_at": None,
            },
        ]
    )
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(subscriptions),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_basic_monthly",
                        "product_id": "product_basic",
                        "plan_code": "basic_monthly",
                        "billing_period": "monthly",
                        "amount": 9900,
                        "entitlements": {"seats": 1},
                        "status": "active",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "product_code": "basic",
                        "product_type": "subscription",
                        "name": "Basic",
                        "status": "active",
                    }
                ]
            )
        ),
        billing_methods=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "bm_1",
                        "user_id": "user_1",
                        "payment_customer_id": "customer_1",
                        "instrument_id": "pinstr_1",
                        "display_name": "현대카드 **** 1234",
                        "provider": "tosspayments",
                        "is_default": True,
                        "status": "active",
                        "method": "카드",
                        "card_company": "현대",
                        "masked_number": "**** **** **** 1234",
                        "billing_key_status": "active",
                    }
                ]
            )
        ),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    first_page = await repository.list_admin_subscriptions(AdminListQuery(limit=2))
    second_page = await repository.list_admin_subscriptions(
        AdminListQuery(
            cursor=encode_cursor(
                {
                    "nextBillingAt": None,
                    "nextBillingAtNull": True,
                    "subscriptionId": "sub_canceled_1",
                }
            ),
            limit=2,
        )
    )

    assert [row.subscription_id for row in first_page] == [
        "sub_active",
        "sub_canceled_1",
    ]
    assert first_page[0].default_billing_method_summary == "카드 1234"
    assert [row.subscription_id for row in second_page] == ["sub_canceled_2"]


async def test_mongo_admin_operations_repository_lists_subscription_failure() -> None:
    payments = FakeCollection(
        [
            {
                "_id": "pay_failed",
                "order_id": "order_failed",
                "amount": 9900,
                "status": "failed",
                "created_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_past_due",
                "retry_scheduled_at": datetime(2026, 6, 11, tzinfo=UTC),
                "failure": {
                    "providerCode": "CARD_DECLINED",
                    "message": "card declined",
                    "retryable": True,
                },
            }
        ]
    )
    invoices = FakeCollection(
        [
            {
                "_id": "inv_failed",
                "user_id": "user_1",
                "payment_id": "pay_failed",
                "status": "issued",
                "issued_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_past_due",
            }
        ]
    )
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(payments),
        invoices=motor_collection_stub(invoices),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_past_due",
                        "user_id": "user_1",
                        "payment_customer_id": "customer_1",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "past_due",
                        "cancel_at_period_end": False,
                        "next_billing_at": datetime(2026, 7, 1, tzinfo=UTC),
                    }
                ]
            )
        ),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_basic_monthly",
                        "product_id": "product_basic",
                        "plan_code": "basic_monthly",
                        "billing_period": "monthly",
                        "amount": 9900,
                        "entitlements": {"seats": 1},
                        "status": "active",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "product_code": "basic",
                        "product_type": "subscription",
                        "name": "Basic",
                        "status": "active",
                    }
                ]
            )
        ),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_admin_subscriptions(AdminListQuery(limit=50))

    assert rows[0].payment_failure == {
        "hasFailure": True,
        "lastInvoiceId": "inv_failed",
        "failureCode": "CARD_DECLINED",
        "retryScheduledAt": datetime(2026, 6, 11, tzinfo=UTC),
        "retryAvailable": True,
    }


async def test_mongo_admin_operations_repository_filters_pending_subscriptions() -> (
    None
):
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(FakeCollection()),
        invoices=motor_collection_stub(FakeCollection()),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_pending",
                        "user_id": "user_1",
                        "payment_customer_id": "customer_1",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "pending",
                        "cancel_at_period_end": False,
                    },
                    {
                        "_id": "sub_active",
                        "user_id": "user_1",
                        "payment_customer_id": "customer_1",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "active",
                        "cancel_at_period_end": False,
                        "next_billing_at": datetime(2026, 7, 1, tzinfo=UTC),
                    },
                ]
            )
        ),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_basic_monthly",
                        "product_id": "product_basic",
                        "plan_code": "basic_monthly",
                        "billing_period": "monthly",
                        "amount": 9900,
                        "entitlements": {"seats": 1},
                        "status": "active",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "product_code": "basic",
                        "product_type": "subscription",
                        "name": "Basic",
                        "status": "active",
                    }
                ]
            )
        ),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_admin_subscriptions(
        AdminListQuery(status="pending", limit=50)
    )

    assert [row.subscription_id for row in rows] == ["sub_pending"]


async def test_mongo_admin_operations_repository_filters_retry_payment_failure() -> (
    None
):
    payments = FakeCollection(
        [
            {
                "_id": "pay_retry",
                "order_id": "order_retry",
                "amount": 9900,
                "status": "failed",
                "created_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_active_retry",
                "retry_scheduled_at": datetime(2026, 6, 11, tzinfo=UTC),
                "failure": {
                    "providerCode": "CARD_DECLINED",
                    "message": "card declined",
                    "retryable": True,
                },
            }
        ]
    )
    invoices = FakeCollection(
        [
            {
                "_id": "inv_retry",
                "user_id": "user_1",
                "payment_id": "pay_retry",
                "status": "issued",
                "issued_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_active_retry",
            }
        ]
    )
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(payments),
        invoices=motor_collection_stub(invoices),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_active_retry",
                        "user_id": "user_1",
                        "payment_customer_id": "customer_1",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "active",
                        "cancel_at_period_end": False,
                        "next_billing_at": datetime(2026, 7, 1, tzinfo=UTC),
                    },
                    {
                        "_id": "sub_active_clean",
                        "user_id": "user_1",
                        "payment_customer_id": "customer_1",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "active",
                        "cancel_at_period_end": False,
                        "next_billing_at": datetime(2026, 7, 2, tzinfo=UTC),
                    },
                ]
            )
        ),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_basic_monthly",
                        "product_id": "product_basic",
                        "plan_code": "basic_monthly",
                        "billing_period": "monthly",
                        "amount": 9900,
                        "entitlements": {"seats": 1},
                        "status": "active",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "product_code": "basic",
                        "product_type": "subscription",
                        "name": "Basic",
                        "status": "active",
                    }
                ]
            )
        ),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_admin_subscriptions(
        AdminListQuery(payment_failure=True, limit=50)
    )

    assert [row.subscription_id for row in rows] == ["sub_active_retry"]
    assert rows[0].payment_failure == {
        "hasFailure": True,
        "lastInvoiceId": "inv_retry",
        "failureCode": "CARD_DECLINED",
        "retryScheduledAt": datetime(2026, 6, 11, tzinfo=UTC),
        "retryAvailable": True,
    }


async def test_mongo_admin_operations_repository_excludes_recovered_old_failure() -> (
    None
):
    payments = FakeCollection(
        [
            {
                "_id": "pay_retry_old_failed",
                "order_id": "order_retry_old_failed",
                "amount": 9900,
                "status": "failed",
                "created_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_recovered",
                "retry_scheduled_at": datetime(2026, 6, 11, tzinfo=UTC),
                "failure": {
                    "providerCode": "CARD_DECLINED",
                    "message": "card declined",
                    "retryable": True,
                },
            },
            {
                "_id": "pay_retry_success",
                "order_id": "order_retry_success",
                "amount": 9900,
                "status": "paid",
                "created_at": datetime(2026, 6, 11, tzinfo=UTC),
                "approved_at": datetime(2026, 6, 11, 0, 1, tzinfo=UTC),
                "subscription_id": "sub_recovered",
            },
        ]
    )
    invoices = FakeCollection(
        [
            {
                "_id": "inv_recovered",
                "user_id": "user_1",
                "payment_id": "pay_retry_success",
                "status": "paid",
                "issued_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_recovered",
            }
        ]
    )
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(payments),
        invoices=motor_collection_stub(invoices),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_recovered",
                        "user_id": "user_1",
                        "payment_customer_id": "customer_1",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "active",
                        "cancel_at_period_end": False,
                        "next_billing_at": datetime(2026, 7, 1, tzinfo=UTC),
                    }
                ]
            )
        ),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_basic_monthly",
                        "product_id": "product_basic",
                        "plan_code": "basic_monthly",
                        "billing_period": "monthly",
                        "amount": 9900,
                        "entitlements": {"seats": 1},
                        "status": "active",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "product_code": "basic",
                        "product_type": "subscription",
                        "name": "Basic",
                        "status": "active",
                    }
                ]
            )
        ),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_admin_subscriptions(
        AdminListQuery(payment_failure=True, limit=50)
    )

    assert rows == []


async def test_mongo_admin_operations_repository_loads_provider_sync_targets() -> (
    None
):
    payments = FakeCollection(
        [
            {
                "_id": "pay_sync",
                "order_id": "order_sync",
                "amount": 9900,
                "status": "failed",
                "created_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_sync",
                "payment_key": "paykey_sync",
            }
        ]
    )
    invoices = FakeCollection(
        [
            {
                "_id": "inv_sync",
                "user_id": "user_1",
                "payment_id": "pay_sync",
                "status": "issued",
                "issued_at": datetime(2026, 6, 10, tzinfo=UTC),
                "subscription_id": "sub_sync",
            }
        ]
    )
    repository = MongoAdminOperationsRepository(
        payments=motor_collection_stub(payments),
        invoices=motor_collection_stub(invoices),
        checkouts=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        operator_audits=motor_collection_stub(FakeCollection()),
    )

    payment, invoice = await repository.get_admin_payment_by_invoice_id("inv_sync")
    payment_by_key = await repository.get_admin_payment_by_payment_key("paykey_sync")
    invoice_by_payment = await repository.get_admin_invoice_by_payment_id("pay_sync")
    latest_failed_payment, latest_failed_invoice = (
        await repository.get_admin_latest_failed_subscription_payment("sub_sync")
    )

    assert payment is not None
    assert payment.id == "pay_sync"
    assert invoice is not None
    assert invoice.id == "inv_sync"
    assert payment_by_key is not None
    assert payment_by_key.id == "pay_sync"
    assert invoice_by_payment is not None
    assert invoice_by_payment.id == "inv_sync"
    assert latest_failed_payment is not None
    assert latest_failed_payment.id == "pay_sync"
    assert latest_failed_invoice is not None
    assert latest_failed_invoice.id == "inv_sync"


async def test_mongo_catalog_repository_filters_active_catalog() -> None:
    products = FakeCollection(
        [
            {
                "_id": "product_basic",
                "product_code": "basic",
                "product_type": "subscription",
                "name": "Basic",
                "status": "active",
            }
        ]
    )
    plans = FakeCollection(
        [
            {
                "_id": "plan_basic_monthly",
                "product_id": "product_basic",
                "plan_code": "basic_monthly",
                "billing_period": "monthly",
                "amount": 9900,
                "entitlements": {"seats": 1},
                "status": "active",
            }
        ]
    )

    rows = await MongoCatalogRepository(
        motor_collection_stub(products),
        motor_collection_stub(plans),
        motor_collection_stub(FakeCollection()),
    ).list_active_subscription_catalog()

    assert len(rows) == 1
    assert rows[0][0].id == "product_basic"
    assert rows[0][1].id == "plan_basic_monthly"


async def test_mongo_catalog_repository_excludes_one_time_products() -> None:
    products = FakeCollection(
        [
            {
                "_id": "product_basic",
                "product_code": "basic",
                "product_type": "subscription",
                "name": "Basic",
                "status": "active",
            },
            {
                "_id": "product_reports",
                "product_code": "reports",
                "product_type": "one_time",
                "name": "Reports",
                "status": "active",
            },
        ]
    )
    plans = FakeCollection(
        [
            {
                "_id": "plan_basic_monthly",
                "product_id": "product_basic",
                "plan_code": "basic_monthly",
                "billing_period": "monthly",
                "amount": 9900,
                "entitlements": {"seats": 1},
                "status": "active",
            },
            {
                "_id": "plan_reports_monthly",
                "product_id": "product_reports",
                "plan_code": "reports_monthly",
                "billing_period": "monthly",
                "amount": 4900,
                "entitlements": {"report_pack": True},
                "status": "active",
            },
        ]
    )

    rows = await MongoCatalogRepository(
        motor_collection_stub(products),
        motor_collection_stub(plans),
        motor_collection_stub(FakeCollection()),
    ).list_active_subscription_catalog()

    assert [row[1].id for row in rows] == ["plan_basic_monthly"]


async def test_mongo_catalog_repository_lists_user_active_subscriptions() -> None:
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_active",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
            },
            {
                "_id": "sub_canceled",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_yearly",
                "product_code": "basic",
                "status": "canceled",
                "cancel_at_period_end": False,
            },
            {
                "_id": "sub_other_user",
                "user_id": "user_2",
                "payment_customer_id": "pcus_2",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
            },
        ]
    )

    rows = await MongoCatalogRepository(
        motor_collection_stub(FakeCollection()),
        motor_collection_stub(FakeCollection()),
        motor_collection_stub(subscriptions),
    ).list_user_active_product_subscriptions("user_1")

    assert [row.id for row in rows] == ["sub_active"]


async def test_mongo_payment_attempt_repository_enforces_checkout_ownership() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    checkouts = FakeCollection()
    checkout_repository = MongoCheckoutRepository(motor_collection_stub(checkouts))
    payment_attempts = MongoPaymentAttemptRepository(
        checkouts=motor_collection_stub(checkouts),
        payments=motor_collection_stub(FakeCollection()),
    )
    checkout = Checkout(
        id="chk_1",
        user_id="user_1",
        payment_customer_id="pcus_1",
        items=[],
        status="ready",
        created_at=now,
    )
    payment = Payment(
        id="pay_1",
        order_id="order_1",
        amount=1000,
        status="ready",
        created_at=now,
        checkout_id="chk_1",
    )

    await checkout_repository.save_checkout(checkout)
    await payment_attempts.save_payment(payment)

    assert checkouts.documents["chk_1"] == {
        "_id": "chk_1",
        "user_id": "user_1",
        "payment_customer_id": "pcus_1",
        "items": [],
        "status": "ready",
        "created_at": now,
    }
    assert await payment_attempts.get_payment_for_user("pay_1", "user_1") == payment
    assert await payment_attempts.get_payment_for_user("pay_1", "user_2") is None


async def test_mongo_checkout_repository_marks_checkout_paid_only_from_ready() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    checkouts = FakeCollection()
    repository = MongoCheckoutRepository(motor_collection_stub(checkouts))
    await repository.save_checkout(
        Checkout(
            id="chk_1",
            user_id="user_1",
            payment_customer_id="pcus_1",
            items=[],
            status="ready",
            created_at=now,
        )
    )

    first = await repository.mark_checkout_paid_if_ready(
        "chk_1",
        "user_1",
        "pay_1",
    )
    second = await repository.mark_checkout_paid_if_ready(
        "chk_1",
        "user_1",
        "pay_2",
    )

    assert first is True
    assert second is False
    assert checkouts.documents["chk_1"]["status"] == "paid"
    assert checkouts.documents["chk_1"]["last_payment_id"] == "pay_1"


async def test_mongo_payment_attempt_repository_counts_checkout_attempts() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    payments = FakeCollection()
    payment_attempts = MongoPaymentAttemptRepository(
        checkouts=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(payments),
    )
    await payment_attempts.save_payment(
        Payment(
            id="pay_1",
            order_id="order_1",
            amount=1000,
            status="ready",
            created_at=now,
            checkout_id="chk_1",
        )
    )
    await payment_attempts.save_payment(
        Payment(
            id="pay_2",
            order_id="order_2",
            amount=1000,
            status="ready",
            created_at=now,
            checkout_id="chk_1",
        )
    )

    assert await payment_attempts.count_payments_for_checkout("chk_1") == 2
    assert await payment_attempts.count_payments_for_checkout("chk_2") == 0


async def test_mongo_payment_attempt_repository_gets_attempt_number() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    payments = FakeCollection()
    payment_attempts = MongoPaymentAttemptRepository(
        checkouts=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(payments),
    )
    await payment_attempts.save_payment(
        Payment(
            id="pay_1",
            order_id="order_1",
            amount=1000,
            status="failed",
            created_at=now,
            checkout_id="chk_1",
        )
    )
    await payment_attempts.save_payment(
        Payment(
            id="pay_2",
            order_id="order_2",
            amount=1000,
            status="ready",
            created_at=now,
            checkout_id="chk_1",
        )
    )

    assert await payment_attempts.get_payment_attempt_no("chk_1", "pay_1") == 1
    assert await payment_attempts.get_payment_attempt_no("chk_1", "pay_2") == 2


async def test_mongo_payment_attempt_repository_counts_user_sku_quantity() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    checkouts = FakeCollection()
    payments = FakeCollection()
    checkout_repository = MongoCheckoutRepository(
        checkouts=motor_collection_stub(checkouts),
    )
    payment_attempts = MongoPaymentAttemptRepository(
        checkouts=motor_collection_stub(checkouts),
        payments=motor_collection_stub(payments),
    )
    await checkout_repository.save_checkout(
        Checkout(
            id="chk_1",
            user_id="user_1",
            payment_customer_id="pcus_1",
            items=[{"skuId": "sku_1", "quantity": 2}],
            status="ready",
            created_at=now,
        )
    )
    await checkout_repository.save_checkout(
        Checkout(
            id="chk_2",
            user_id="user_2",
            payment_customer_id="pcus_2",
            items=[{"skuId": "sku_1", "quantity": 5}],
            status="ready",
            created_at=now,
        )
    )
    await payment_attempts.save_payment(
        Payment(
            id="pay_1",
            order_id="order_1",
            amount=1000,
            status="ready",
            created_at=now,
            checkout_id="chk_1",
        )
    )
    await payment_attempts.save_payment(
        Payment(
            id="pay_2",
            order_id="order_2",
            amount=1000,
            status="failed",
            created_at=now,
            checkout_id="chk_1",
        )
    )
    await payment_attempts.save_payment(
        Payment(
            id="pay_3",
            order_id="order_3",
            amount=1000,
            status="ready",
            created_at=now,
            checkout_id="chk_2",
        )
    )

    assert (
        await payment_attempts.count_user_payment_quantity_for_sku(
            "user_1",
            "sku_1",
            {"ready", "paid"},
        )
        == 2
    )


async def test_mongo_payment_attempt_repository_omits_none_payment_fields() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    payments = FakeCollection()
    repository = MongoPaymentAttemptRepository(
        checkouts=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(payments),
    )

    await repository.save_payment(
        Payment(
            id="pay_1",
            order_id="order_1",
            amount=1000,
            status="ready",
            created_at=now,
            checkout_id="chk_1",
            billing_method_id="bm_1",
            payment_key=None,
            approved_at=None,
        )
    )

    document = payments.documents["pay_1"]
    assert "payment_key" not in document
    assert "approved_at" not in document
    assert document["billing_method_id"] == "bm_1"


async def test_mongo_payment_customer_repository_loads_active_customer() -> None:
    customers = FakeCollection()
    repository = MongoPaymentCustomerRepository(motor_collection_stub(customers))
    revoked_at = datetime(2026, 6, 11, tzinfo=UTC)
    customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    revoked_customer = PaymentCustomer(
        id="pcus_revoked",
        user_id="user_revoked",
        provider="tosspayments",
        customer_key="pcus_key_revoked",
        status="revoked",
        revoked_at=revoked_at,
    )

    await repository.save_payment_customer(customer)
    await repository.save_payment_customer(revoked_customer)

    assert customers.documents["pcus_1"] == {
        "_id": "pcus_1",
        "user_id": "user_1",
        "provider": "tosspayments",
        "customer_key": "pcus_key_1",
        "status": "active",
    }
    assert customers.documents["pcus_revoked"]["revoked_at"] == revoked_at
    assert await repository.get_active_payment_customer_for_user("user_1") == customer
    assert await repository.get_active_payment_customer_for_user("user_2") is None


async def test_mongo_billing_auth_saves_customer_key_with_documented_fields() -> None:
    customers = FakeCollection()
    repository = MongoBillingAuthRepository(
        billing_auths=motor_collection_stub(FakeCollection()),
        payment_customers=motor_collection_stub(customers),
        billing_methods=motor_collection_stub(FakeCollection()),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )

    await repository.save_customer_key_for_user("user_1", "pcus_key_1")

    document = customers.documents["pcus_for_user_1"]
    assert document == {
        "_id": "pcus_for_user_1",
        "user_id": "user_1",
        "provider": "tosspayments",
        "customer_key": "pcus_key_1",
        "status": "active",
    }
    assert await repository.get_customer_key_for_user("user_1") == "pcus_key_1"


async def test_mongo_idempotency_repository_looks_up_by_scope_and_hash() -> (
    None
):
    now = datetime(2026, 6, 10, tzinfo=UTC)
    documents = FakeCollection()
    idempotency_keys = MongoIdempotencyKeyRepository(
        motor_collection_stub(documents)
    )
    key = IdempotencyKey(
        id="idem_1",
        scope="payments-orders",
        key_hash="hash",
        request_hash="request",
        status="succeeded",
        created_at=now,
        updated_at=now,
        expires_at=now,
    )

    await idempotency_keys.save_idempotency_key(key)

    assert documents.documents["idem_1"] == {
        "_id": "idem_1",
        "scope": "payments-orders",
        "key_hash": "hash",
        "request_hash": "request",
        "status": "succeeded",
        "created_at": now,
        "updated_at": now,
        "expires_at": now,
    }
    assert await idempotency_keys.find_idempotency_key("payments-orders", "hash") == key
    assert await idempotency_keys.find_idempotency_key("other", "hash") is None


async def test_mongo_idempotency_repository_looks_up_by_resource() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    documents = FakeCollection()
    idempotency_keys = MongoIdempotencyKeyRepository(
        motor_collection_stub(documents)
    )
    key = IdempotencyKey(
        id="idem_1",
        scope="billing-issue",
        key_hash="hash",
        request_hash="request",
        status="succeeded",
        created_at=now,
        updated_at=now,
        expires_at=now,
        resource_type="billing_auth",
        resource_id="bauth_123",
        response_status=201,
        response_body={"billingMethodId": "bm_123"},
    )

    await idempotency_keys.save_idempotency_key(key)

    assert documents.documents["idem_1"]["response_body"] == {
        "billingMethodId": "bm_123"
    }
    assert (
        await idempotency_keys.find_idempotency_key_by_resource(
            "billing-issue",
            "billing_auth",
            "bauth_123",
        )
        == key
    )
    assert (
        await idempotency_keys.find_idempotency_key_by_resource(
            "billing-issue",
            "billing_auth",
            "bauth_other",
        )
        is None
    )


async def test_mongo_idempotency_repository_looks_up_succeeded_resource() -> (
    None
):
    now = datetime(2026, 6, 10, tzinfo=UTC)
    documents = FakeCollection()
    idempotency_keys = MongoIdempotencyKeyRepository(
        motor_collection_stub(documents)
    )
    failed_key = IdempotencyKey(
        id="idem_failed",
        scope="subscriptions-confirm",
        key_hash="failed_hash",
        request_hash="failed_request",
        status="failed",
        created_at=now,
        updated_at=now,
        expires_at=now,
        resource_type="subscription",
        resource_id="sub_123",
        response_status=402,
        response_body={"subscriptionId": "sub_123", "status": "pending"},
    )
    succeeded_key = IdempotencyKey(
        id="idem_succeeded",
        scope="subscriptions-confirm",
        key_hash="succeeded_hash",
        request_hash="succeeded_request",
        status="succeeded",
        created_at=now,
        updated_at=now,
        expires_at=now,
        resource_type="subscription",
        resource_id="sub_123",
        response_status=200,
        response_body={"subscriptionId": "sub_123", "status": "active"},
    )

    await idempotency_keys.save_idempotency_key(failed_key)
    await idempotency_keys.save_idempotency_key(succeeded_key)

    assert (
        await idempotency_keys.find_succeeded_idempotency_key_by_resource(
            "subscriptions-confirm",
            "subscription",
            "sub_123",
        )
        == succeeded_key
    )
    assert (
        await idempotency_keys.find_succeeded_idempotency_key_by_resource(
            "subscriptions-confirm",
            "subscription",
            "sub_other",
        )
        is None
    )


async def test_mongo_payment_cancel_request_repository_loads_by_payment_and_key() -> (
    None
):
    now = datetime(2026, 6, 10, tzinfo=UTC)
    cancel_requests = FakeCollection()
    repository = MongoPaymentCancelRequestRepository(
        motor_collection_stub(cancel_requests)
    )
    cancel_request = PaymentCancelRequest(
        id="pcancel_1",
        payment_id="pay_1",
        idempotency_key_hash="hash",
        status="pending",
        cancel_amount=12000,
        cancel_reason="customer_request",
        requested_by="user",
        requested_user_id="user_1",
        created_at=now,
        updated_at=now,
    )

    await repository.save_payment_cancel_request(cancel_request)

    assert cancel_requests.documents["pcancel_1"] == {
        "_id": "pcancel_1",
        "payment_id": "pay_1",
        "idempotency_key_hash": "hash",
        "status": "pending",
        "cancel_amount": 12000,
        "cancel_reason": "customer_request",
        "requested_by": "user",
        "requested_user_id": "user_1",
        "created_at": now,
        "updated_at": now,
    }
    assert (
        await repository.find_payment_cancel_request("pay_1", "hash")
        == cancel_request
    )
    assert await repository.find_payment_cancel_request("pay_1", "other") is None


async def test_mongo_one_time_sku_repository_loads_only_active_skus() -> None:
    one_time_skus = MongoOneTimeSkuRepository(
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_reports",
                        "product_code": "reports",
                        "product_type": "one_time",
                        "name": "Reports",
                        "status": "active",
                    }
                ]
            )
        ),
        one_time_skus=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sku_report_pack_100",
                        "product_id": "product_reports",
                        "sku_code": "REPORT_PACK_100",
                        "amount": 25000,
                        "stock_policy": "unlimited",
                        "status": "active",
                    }
                ]
            )
        ),
    )

    sku = await one_time_skus.get_active_one_time_sku("sku_report_pack_100")

    assert sku == OneTimeSku(
        id="sku_report_pack_100",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=25000,
        stock_policy="unlimited",
        status="active",
    )


async def test_mongo_one_time_sku_repository_rejects_paused_reservation() -> None:
    skus = FakeCollection(
        [
            {
                "_id": "sku_report_pack_100",
                "product_id": "product_reports",
                "sku_code": "REPORT_PACK_100",
                "amount": 25000,
                "stock_policy": "limited",
                "total_stock": 10,
                "reserved_stock": 0,
                "sold_stock": 0,
                "status": "active",
            }
        ]
    )
    one_time_skus = MongoOneTimeSkuRepository(
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_reports",
                        "product_code": "reports",
                        "product_type": "one_time",
                        "name": "Reports",
                        "status": "paused",
                    }
                ]
            )
        ),
        one_time_skus=motor_collection_stub(skus),
    )
    stale_active_sku = OneTimeSku(
        id="sku_report_pack_100",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=25000,
        stock_policy="limited",
        total_stock=10,
        reserved_stock=0,
        sold_stock=0,
        status="active",
    )

    reserved = await one_time_skus.reserve_one_time_sku_stock(stale_active_sku, 2)

    assert reserved is False
    assert skus.documents["sku_report_pack_100"]["reserved_stock"] == 0


async def test_mongo_one_time_sku_repository_rejects_paused_product_unlimited() -> None:
    one_time_skus = MongoOneTimeSkuRepository(
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_reports",
                        "product_code": "reports",
                        "product_type": "one_time",
                        "name": "Reports",
                        "status": "paused",
                    }
                ]
            )
        ),
        one_time_skus=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sku_report_pack_100",
                        "product_id": "product_reports",
                        "sku_code": "REPORT_PACK_100",
                        "amount": 25000,
                        "stock_policy": "unlimited",
                        "status": "active",
                    }
                ]
            )
        ),
    )
    stale_active_sku = OneTimeSku(
        id="sku_report_pack_100",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=25000,
        stock_policy="unlimited",
        status="active",
    )

    assert (
        await one_time_skus.reserve_one_time_sku_stock(stale_active_sku, 2)
        is False
    )


async def test_mongo_subscription_account_repository_loads_user_account() -> (
    None
):
    now = datetime(2026, 6, 10, tzinfo=UTC)
    repository = MongoSubscriptionAccountRepository(
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_1",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "active",
                        "cancel_at_period_end": False,
                        "current_period_start_at": now,
                        "current_period_end_at": now,
                        "next_billing_at": now,
                    },
                    {
                        "_id": "sub_other",
                        "user_id": "user_2",
                        "payment_customer_id": "pcus_2",
                        "plan_id": "plan_basic_monthly",
                        "product_code": "basic",
                        "status": "active",
                        "cancel_at_period_end": False,
                    },
                ]
            )
        ),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_basic_monthly",
                        "product_id": "product_basic",
                        "plan_code": "basic_monthly",
                        "billing_period": "monthly",
                        "amount": 9900,
                        "entitlements": {"seats": 1},
                        "status": "active",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "product_code": "basic",
                        "product_type": "subscription",
                        "name": "Basic",
                        "status": "active",
                    }
                ]
            )
        ),
        billing_methods=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "bm_123",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "instrument_id": "pinstr_1",
                        "display_name": "현대카드 **** 1234",
                        "provider": "tosspayments",
                        "is_default": True,
                        "status": "active",
                    }
                ]
            )
        ),
        payment_instruments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pinstr_1",
                        "payment_customer_id": "pcus_1",
                        "provider": "tosspayments",
                        "billing_key": "encrypted",
                        "billing_key_hash": "hash",
                        "status": "active",
                    }
                ]
            )
        ),
    )

    rows = await repository.list_user_subscription_records("user_1")
    billing_method = await repository.get_default_billing_method("user_1")

    assert len(rows) == 1
    assert rows[0].subscription_id == "sub_1"
    assert rows[0].plan_name == "Basic 월간"
    assert billing_method is not None
    assert billing_method.billing_method_id == "bm_123"


async def test_mongo_subscription_account_repository_hides_revoked_default_method() -> (
    None
):
    repository = MongoSubscriptionAccountRepository(
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "bm_revoked",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "instrument_id": "pinstr_revoked",
                        "display_name": "현대카드 **** 1234",
                        "provider": "tosspayments",
                        "is_default": True,
                        "status": "active",
                    }
                ]
            )
        ),
        payment_instruments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pinstr_revoked",
                        "payment_customer_id": "pcus_1",
                        "provider": "tosspayments",
                        "billing_key": "encrypted",
                        "billing_key_hash": "hash",
                        "status": "revoked",
                    }
                ]
            )
        ),
    )

    billing_method = await repository.get_default_billing_method("user_1")

    assert billing_method is None


async def test_mongo_subscription_account_repository_cancels_and_resumes() -> None:
    current_period_end = datetime(2026, 7, 8, tzinfo=UTC)
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
                "next_billing_at": current_period_end,
                "current_period_start_at": datetime(2026, 6, 8, tzinfo=UTC),
                "current_period_end_at": current_period_end,
            }
        ]
    )
    repository = MongoSubscriptionAccountRepository(
        subscriptions=motor_collection_stub(subscriptions),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
    )

    canceled = await repository.schedule_subscription_cancel_at_period_end(
        "sub_123",
        "user_1",
        datetime(2026, 6, 10, tzinfo=UTC),
    )
    resumed = await repository.resume_cancel_scheduled_subscription(
        "sub_123",
        "user_1",
        datetime(2026, 6, 11, tzinfo=UTC),
    )

    assert canceled.status == "cancel_scheduled"
    assert canceled.cancel_at == current_period_end
    assert canceled.next_billing_at is None
    assert resumed.status == "active"
    assert resumed.cancel_at is None
    assert resumed.next_billing_at == current_period_end


async def test_mongo_subscription_account_repository_rejects_stale_cancel() -> None:
    current_period_end = datetime(2026, 7, 8, tzinfo=UTC)
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "cancel_scheduled",
                "cancel_at_period_end": True,
                "cancel_at": current_period_end,
                "access_until": current_period_end,
                "current_period_start_at": datetime(2026, 6, 8, tzinfo=UTC),
                "current_period_end_at": current_period_end,
            }
        ]
    )
    repository = MongoSubscriptionAccountRepository(
        subscriptions=motor_collection_stub(subscriptions),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(LookupError, match="not cancelable"):
        await repository.schedule_subscription_cancel_at_period_end(
            "sub_123",
            "user_1",
            datetime(2026, 6, 10, tzinfo=UTC),
        )


async def test_mongo_subscription_account_repository_rejects_stale_resume() -> None:
    current_period_end = datetime(2026, 7, 8, tzinfo=UTC)
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
                "next_billing_at": current_period_end,
                "current_period_start_at": datetime(2026, 6, 8, tzinfo=UTC),
                "current_period_end_at": current_period_end,
            }
        ]
    )
    repository = MongoSubscriptionAccountRepository(
        subscriptions=motor_collection_stub(subscriptions),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(LookupError, match="not resumable"):
        await repository.resume_cancel_scheduled_subscription(
            "sub_123",
            "user_1",
            datetime(2026, 6, 11, tzinfo=UTC),
        )


async def test_mongo_billing_method_repository_loads_active_user_methods() -> None:
    now = datetime(2026, 6, 8, 10, 15, tzinfo=UTC)
    repository = MongoBillingMethodRepository(
        billing_methods=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "bm_123",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "instrument_id": "pinstr_1",
                        "display_name": "현대카드 **** 1234",
                        "provider": "tosspayments",
                        "is_default": True,
                        "status": "active",
                        "method": "카드",
                        "card_company": "현대",
                        "masked_number": "**** **** **** 1234",
                        "billing_key_status": "active",
                        "created_at": now,
                    },
                    {
                        "_id": "bm_deleted",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "instrument_id": "pinstr_2",
                        "display_name": "삭제된 카드",
                        "provider": "tosspayments",
                        "is_default": False,
                        "status": "deleted",
                    },
                    {
                        "_id": "bm_other",
                        "user_id": "user_2",
                        "payment_customer_id": "pcus_2",
                        "instrument_id": "pinstr_3",
                        "display_name": "다른 회원 카드",
                        "provider": "tosspayments",
                        "is_default": False,
                        "status": "active",
                    },
                ]
            )
        ),
        payment_instruments=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_1",
                        "user_id": "user_1",
                        "status": "active",
                    },
                    {
                        "_id": "sub_past_due",
                        "user_id": "user_1",
                        "status": "past_due",
                    },
                    {
                        "_id": "sub_cancel_scheduled",
                        "user_id": "user_1",
                        "status": "cancel_scheduled",
                    },
                    {
                        "_id": "sub_pending",
                        "user_id": "user_1",
                        "status": "pending",
                    },
                    {
                        "_id": "sub_cancel",
                        "user_id": "user_1",
                        "status": "canceled",
                    },
                ]
            )
        ),
    )

    methods = await repository.list_active_billing_methods_for_user("user_1")
    active_count = await repository.count_active_subscriptions_for_user("user_1")

    assert len(methods) == 1
    assert methods[0].billing_method_id == "bm_123"
    assert methods[0].masked_card_number == "**** **** **** 1234"
    assert active_count == 4


async def test_mongo_billing_method_repository_uses_instrument_status() -> None:
    repository = MongoBillingMethodRepository(
        billing_methods=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "bm_123",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "instrument_id": "pinstr_1",
                        "display_name": "현대카드 **** 1234",
                        "provider": "tosspayments",
                        "is_default": False,
                        "status": "active",
                        "method": "카드",
                        "card_company": "현대",
                        "masked_number": "**** **** **** 1234",
                        "billing_key_status": "active",
                        "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
                    },
                ]
            )
        ),
        payment_instruments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pinstr_1",
                        "payment_customer_id": "pcus_1",
                        "provider": "tosspayments",
                        "billing_key": "encrypted",
                        "billing_key_hash": "hash",
                        "status": "revoked",
                    }
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
    )

    method = await repository.get_billing_method_for_user("bm_123", "user_1")

    assert method is not None
    assert method.billing_key_status == "revoked"


async def test_mongo_billing_method_repository_changes_default_method() -> None:
    billing_methods = FakeCollection(
        [
            {
                "_id": "bm_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_1",
                "display_name": "현대카드 **** 1234",
                "provider": "tosspayments",
                "is_default": True,
                "status": "active",
                "method": "카드",
                "card_company": "현대",
                "masked_number": "**** **** **** 1234",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
            },
            {
                "_id": "bm_456",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_2",
                "display_name": "신한카드 **** 5678",
                "provider": "tosspayments",
                "is_default": False,
                "status": "active",
                "method": "카드",
                "card_company": "신한",
                "masked_number": "**** **** **** 5678",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
            },
        ]
    )
    repository = MongoBillingMethodRepository(
        billing_methods=motor_collection_stub(billing_methods),
        payment_instruments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pinstr_2",
                        "payment_customer_id": "pcus_1",
                        "provider": "tosspayments",
                        "billing_key": "encrypted",
                        "billing_key_hash": "hash",
                        "status": "active",
                    }
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
    )
    changed_at = datetime(2026, 6, 10, tzinfo=UTC)

    previous_default_id = await repository.set_default_billing_method_for_user(
        "bm_456",
        "user_1",
        changed_at,
    )

    assert previous_default_id == "bm_123"
    assert billing_methods.documents["bm_123"]["is_default"] is False
    assert billing_methods.documents["bm_456"]["is_default"] is True
    assert billing_methods.documents["bm_456"]["default_changed_at"] == changed_at


async def test_mongo_billing_method_repository_rechecks_defaultable_instrument() -> (
    None
):
    billing_methods = FakeCollection(
        [
            {
                "_id": "bm_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_1",
                "display_name": "현대카드 **** 1234",
                "provider": "tosspayments",
                "is_default": True,
                "status": "active",
                "method": "카드",
                "card_company": "현대",
                "masked_number": "**** **** **** 1234",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
            },
            {
                "_id": "bm_456",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_2",
                "display_name": "신한카드 **** 5678",
                "provider": "tosspayments",
                "is_default": False,
                "status": "active",
                "method": "카드",
                "card_company": "신한",
                "masked_number": "**** **** **** 5678",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
            },
        ]
    )
    repository = MongoBillingMethodRepository(
        billing_methods=motor_collection_stub(billing_methods),
        payment_instruments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pinstr_2",
                        "payment_customer_id": "pcus_1",
                        "provider": "tosspayments",
                        "billing_key": "encrypted",
                        "billing_key_hash": "hash",
                        "status": "revoked",
                    }
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(LookupError):
        await repository.set_default_billing_method_for_user(
            "bm_456",
            "user_1",
            datetime(2026, 6, 10, tzinfo=UTC),
        )

    assert billing_methods.documents["bm_123"]["is_default"] is True
    assert billing_methods.documents["bm_456"]["is_default"] is False


async def test_mongo_billing_method_repository_loads_method_owner() -> None:
    billing_methods = FakeCollection(
        [
            {
                "_id": "bm_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_1",
                "display_name": "현대카드 **** 1234",
                "provider": "tosspayments",
                "is_default": True,
                "status": "inactive",
                "method": "카드",
                "card_company": "현대",
                "masked_number": "**** **** **** 1234",
                "billing_key_status": "revoked",
                "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
            }
        ]
    )
    repository = MongoBillingMethodRepository(
        motor_collection_stub(billing_methods),
        motor_collection_stub(FakeCollection()),
        motor_collection_stub(FakeCollection()),
    )

    assert await repository.get_billing_method_owner("bm_123") == "user_1"
    assert await repository.get_billing_method_owner("bm_missing") is None


async def test_mongo_billing_method_default_uow_wraps_default_change() -> None:
    database = FakeDatabase()
    database.billing_methods = FakeCollection(
        [
            {
                "_id": "bm_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_1",
                "display_name": "현대카드 **** 1234",
                "provider": "tosspayments",
                "is_default": True,
                "status": "active",
                "method": "카드",
                "card_company": "현대",
                "masked_number": "**** **** **** 1234",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
            },
            {
                "_id": "bm_456",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_2",
                "display_name": "신한카드 **** 5678",
                "provider": "tosspayments",
                "is_default": False,
                "status": "active",
                "method": "카드",
                "card_company": "신한",
                "masked_number": "**** **** **** 5678",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
            },
        ]
    )
    database.payment_instruments = FakeCollection(
        [
            {
                "_id": "pinstr_2",
                "payment_customer_id": "pcus_1",
                "provider": "tosspayments",
                "billing_key": "encrypted",
                "billing_key_hash": "hash",
                "status": "active",
            }
        ]
    )
    changed_at = datetime(2026, 6, 10, tzinfo=UTC)

    async with MongoBillingMethodDefaultUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        previous_default_id = await (
            uow.billing_methods.set_default_billing_method_for_user(
                "bm_456",
                "user_1",
                changed_at,
            )
        )

    [session] = database.client.sessions
    assert previous_default_id == "bm_123"
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.billing_methods.documents["bm_123"]["is_default"] is False
    assert database.billing_methods.documents["bm_456"]["is_default"] is True
    assert "billing_key_status" not in database.billing_methods.documents["bm_456"]
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.billing_methods.calls
        if method in {"find_one", "update_many", "update_one"}
    )


async def test_mongo_billing_method_default_uow_aborts_stale_target() -> None:
    database = FakeDatabase()
    database.billing_methods = FakeCollection(
        [
            {
                "_id": "bm_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_1",
                "display_name": "현대카드 **** 1234",
                "provider": "tosspayments",
                "is_default": True,
                "status": "active",
                "method": "카드",
                "card_company": "현대",
                "masked_number": "**** **** **** 1234",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
            },
            {
                "_id": "bm_456",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_2",
                "display_name": "신한카드 **** 5678",
                "provider": "tosspayments",
                "is_default": False,
                "status": "inactive",
                "method": "카드",
                "card_company": "신한",
                "masked_number": "**** **** **** 5678",
                "billing_key_status": "revoked",
                "created_at": datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
            },
        ]
    )

    with pytest.raises(LookupError, match="not defaultable"):
        async with MongoBillingMethodDefaultUnitOfWorkFactory(
            motor_database_stub(database)
        )() as uow:
            await uow.billing_methods.set_default_billing_method_for_user(
                "bm_456",
                "user_1",
                datetime(2026, 6, 10, tzinfo=UTC),
            )

    [session] = database.client.sessions
    assert session.started is True
    assert session.committed is False
    assert session.aborted is True
    assert session.ended is True


async def test_mongo_billing_auth_counts_only_active_instruments() -> None:
    repository = MongoBillingAuthRepository(
        billing_auths=motor_collection_stub(FakeCollection()),
        payment_customers=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "bm_revoked",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "instrument_id": "pinstr_revoked",
                        "display_name": "현대카드 **** 1111",
                        "provider": "tosspayments",
                        "is_default": False,
                        "status": "active",
                        "method": "카드",
                        "card_company": "현대",
                        "masked_number": "**** **** **** 1111",
                        "billing_key_status": "active",
                        "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
                    },
                    {
                        "_id": "bm_active",
                        "user_id": "user_1",
                        "payment_customer_id": "pcus_1",
                        "instrument_id": "pinstr_active",
                        "display_name": "신한카드 **** 2222",
                        "provider": "tosspayments",
                        "is_default": False,
                        "status": "active",
                        "method": "카드",
                        "card_company": "신한",
                        "masked_number": "**** **** **** 2222",
                        "billing_key_status": "active",
                        "created_at": datetime(2026, 6, 9, 10, 15, tzinfo=UTC),
                    },
                ]
            )
        ),
        payment_instruments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pinstr_revoked",
                        "payment_customer_id": "pcus_1",
                        "provider": "tosspayments",
                        "billing_key": "encrypted_old",
                        "billing_key_hash": "hash_old",
                        "status": "revoked",
                    },
                    {
                        "_id": "pinstr_active",
                        "payment_customer_id": "pcus_1",
                        "provider": "tosspayments",
                        "billing_key": "encrypted_new",
                        "billing_key_hash": "hash_new",
                        "status": "active",
                    },
                ]
            )
        ),
    )

    active_count = await repository.count_active_billing_methods_for_user("user_1")

    assert active_count == 1


async def test_mongo_billing_auth_saves_documented_state_fields() -> None:
    billing_auths = FakeCollection()
    repository = MongoBillingAuthRepository(
        billing_auths=motor_collection_stub(billing_auths),
        payment_customers=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )
    expires_at = datetime(2026, 6, 10, 0, 30, tzinfo=UTC)

    await repository.save_billing_auth(
        BillingAuth(
            id="bauth_123",
            user_id="user_1",
            payment_customer_id="pcus_1",
            customer_key_snapshot="pcus_key_1",
            set_as_default=True,
            status="ready",
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
            expires_at=expires_at,
        )
    )

    assert billing_auths.documents["bauth_123"] == {
        "_id": "bauth_123",
        "user_id": "user_1",
        "payment_customer_id": "pcus_1",
        "customer_key_snapshot": "pcus_key_1",
        "set_as_default": True,
        "status": "ready",
        "expires_at": expires_at,
    }
    loaded = await repository.get_billing_auth_for_user("bauth_123", "user_1")
    assert loaded is not None
    assert loaded.success_url == ""
    assert loaded.fail_url == ""
    assert loaded.created_at is None


async def test_mongo_billing_auth_omits_empty_billing_method_fields() -> None:
    billing_methods = FakeCollection()
    repository = MongoBillingAuthRepository(
        billing_auths=motor_collection_stub(FakeCollection()),
        payment_customers=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(billing_methods),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )

    await repository.save_billing_method(
        BillingMethod(
            id="bm_123",
            user_id="user_1",
            payment_customer_id="pcus_1",
            instrument_id="pinstr_123",
            display_name="카드",
            provider="tosspayments",
            is_default=False,
            status="active",
        )
    )

    assert billing_methods.documents["bm_123"] == {
        "_id": "bm_123",
        "user_id": "user_1",
        "payment_customer_id": "pcus_1",
        "instrument_id": "pinstr_123",
        "display_name": "카드",
        "provider": "tosspayments",
        "is_default": False,
        "status": "active",
        "method": "카드",
        "card_company": "",
    }


async def test_mongo_billing_auth_issue_uow_wraps_default_writes() -> None:
    database = FakeDatabase()
    database.billing_methods = FakeCollection(
        [
            {
                "_id": "bm_old",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_old",
                "display_name": "현대카드 **** 1111",
                "provider": "tosspayments",
                "is_default": True,
                "status": "active",
                "method": "카드",
                "card_company": "현대",
                "masked_number": "**** **** **** 1111",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
            }
        ]
    )
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        customer_key_snapshot="pcus_key_1",
        set_as_default=True,
        status="issued",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
    )
    instrument = PaymentInstrument(
        id="pinstr_new",
        payment_customer_id="pcus_1",
        provider="tosspayments",
        billing_key="encrypted",
        billing_key_hash="hash",
        status="active",
        provider_raw={"provider": "tosspayments"},
    )
    billing_method = BillingMethod(
        id="bm_new",
        user_id="user_1",
        payment_customer_id="pcus_1",
        instrument_id=instrument.id,
        display_name="신한카드 **** 2222",
        provider="tosspayments",
        is_default=True,
        status="active",
        method="카드",
        card_company="신한",
        billing_key_status="active",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        masked_number="**** **** **** 2222",
    )
    idempotency_key = IdempotencyKey(
        id="idem_1",
        scope="billing-issue",
        key_hash="hash",
        request_hash="request_hash",
        status="succeeded",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        updated_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 11, tzinfo=UTC),
        resource_type="billing_method",
        resource_id=billing_method.id,
    )

    async with MongoBillingAuthIssueUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        await uow.billing_auths.clear_default_billing_methods_for_user("user_1")
        await uow.billing_auths.save_payment_instrument(instrument)
        await uow.billing_auths.save_billing_method(billing_method)
        await uow.billing_auths.save_billing_auth(billing_auth)
        await uow.idempotency_keys.save_idempotency_key(idempotency_key)

    [session] = database.client.sessions
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.billing_methods.documents["bm_old"]["is_default"] is False
    assert database.billing_methods.documents["bm_new"]["is_default"] is True
    assert "billing_key_status" not in database.billing_methods.documents["bm_new"]
    assert database.payment_instruments.documents["pinstr_new"]["billing_key"] == (
        "encrypted"
    )
    assert "revoked_at" not in database.payment_instruments.documents["pinstr_new"]
    assert database.billing_auths.documents["bauth_123"]["status"] == "issued"
    assert database.idempotency_keys.documents["idem_1"]["status"] == "succeeded"
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.billing_methods.calls
        if method in {"update_many", "replace_one"}
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.payment_instruments.calls
        if method == "replace_one"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.billing_auths.calls
        if method == "replace_one"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.idempotency_keys.calls
        if method == "replace_one"
    )


async def test_mongo_billing_method_delete_uow_wraps_delete_audit() -> None:
    database = FakeDatabase()
    database.billing_methods = FakeCollection(
        [
            {
                "_id": "bm_456",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_2",
                "display_name": "신한카드 **** 5678",
                "provider": "tosspayments",
                "is_default": False,
                "status": "active",
                "method": "카드",
                "card_company": "신한",
                "masked_number": "**** **** **** 5678",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
            },
        ]
    )
    database.payment_instruments = FakeCollection(
        [{"_id": "pinstr_2", "status": "active"}]
    )
    deleted_at = datetime(2026, 6, 10, tzinfo=UTC)

    async with MongoBillingMethodDeleteUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        await uow.billing_methods.deactivate_billing_method_for_user(
            "bm_456",
            "user_1",
            deleted_at,
        )
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id="idem_1",
                scope="billing-method-delete",
                key_hash="key_hash",
                request_hash="request_hash",
                status="succeeded",
                created_at=deleted_at,
                updated_at=deleted_at,
                expires_at=deleted_at,
                resource_type="billing_method",
                resource_id="bm_456",
            )
        )
        await uow.operator_audits.save_operator_audit(
            OperatorAudit(
                id="oaudit_1",
                operator_id="user_1",
                action="billing_method.delete",
                target_type="billing_method",
                target_id="bm_456",
                previous_state={"status": "active"},
                next_state={"status": "inactive"},
                reason_code="user_request",
                result="succeeded",
                created_at=deleted_at,
            )
        )

    [session] = database.client.sessions
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.billing_methods.documents["bm_456"]["status"] == "inactive"
    assert "billing_key_status" not in database.billing_methods.documents["bm_456"]
    assert database.payment_instruments.documents["pinstr_2"]["status"] == "revoked"
    assert database.idempotency_keys.documents["idem_1"]["scope"] == (
        "billing-method-delete"
    )
    assert database.operator_audits.documents["oaudit_1"]["action"] == (
        "billing_method.delete"
    )
    assert "idempotency_key_id" not in database.operator_audits.documents["oaudit_1"]
    assert "reason_message" not in database.operator_audits.documents["oaudit_1"]
    assert "request_ip" not in database.operator_audits.documents["oaudit_1"]
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.operator_audits.calls
        if method == "replace_one"
    )


async def test_mongo_billing_method_delete_rejects_stale_active_state() -> None:
    deleted_at = datetime(2026, 6, 10, tzinfo=UTC)
    billing_methods = FakeCollection(
        [
            {
                "_id": "bm_456",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_2",
                "display_name": "신한카드 **** 5678",
                "provider": "tosspayments",
                "is_default": False,
                "status": "inactive",
                "method": "카드",
                "card_company": "신한",
                "masked_number": "**** **** **** 5678",
                "created_at": datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
            },
        ]
    )
    repository = MongoBillingMethodRepository(
        billing_methods=motor_collection_stub(billing_methods),
        subscriptions=motor_collection_stub(FakeCollection()),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(LookupError, match="not deletable"):
        await repository.deactivate_billing_method_for_user(
            "bm_456",
            "user_1",
            deleted_at,
        )

    assert billing_methods.documents["bm_456"]["status"] == "inactive"


async def test_mongo_billing_retry_repository_counts_failed_cycle_attempts() -> None:
    repository = MongoBillingRetryRepository(
        invoices=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_failed_1",
                        "subscription_id": "sub_1",
                        "billing_cycle_key": "cycle_1",
                        "status": "failed",
                    },
                    {
                        "_id": "pay_failed_2",
                        "subscription_id": "sub_1",
                        "billing_cycle_key": "cycle_1",
                        "status": "failed",
                    },
                    {
                        "_id": "pay_paid",
                        "subscription_id": "sub_1",
                        "billing_cycle_key": "cycle_1",
                        "status": "paid",
                    },
                    {
                        "_id": "pay_other_cycle",
                        "subscription_id": "sub_1",
                        "billing_cycle_key": "cycle_2",
                        "status": "failed",
                    },
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )

    count = await repository.count_failed_payments_for_billing_cycle(
        "sub_1",
        "cycle_1",
    )

    assert count == 2


async def test_mongo_billing_retry_repository_gets_latest_failed_cycle_payment() -> (
    None
):
    repository = MongoBillingRetryRepository(
        invoices=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_latest_failed",
                        "order_id": "ord_latest_failed",
                        "amount": 9900,
                        "subscription_id": "sub_1",
                        "billing_cycle_key": "cycle_1",
                        "status": "failed",
                        "created_at": datetime(2026, 6, 10, tzinfo=UTC),
                        "retry_scheduled_at": datetime(2026, 6, 11, tzinfo=UTC),
                    },
                    {
                        "_id": "pay_original_failed",
                        "order_id": "ord_original_failed",
                        "amount": 9900,
                        "subscription_id": "sub_1",
                        "billing_cycle_key": "cycle_1",
                        "status": "failed",
                        "created_at": datetime(2026, 6, 9, tzinfo=UTC),
                        "retry_scheduled_at": datetime(2026, 6, 10, tzinfo=UTC),
                    },
                    {
                        "_id": "pay_other_cycle",
                        "order_id": "ord_other_cycle",
                        "amount": 9900,
                        "subscription_id": "sub_1",
                        "billing_cycle_key": "cycle_2",
                        "status": "failed",
                        "created_at": datetime(2026, 6, 12, tzinfo=UTC),
                    },
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )

    payment = await repository.get_latest_failed_payment_for_billing_cycle(
        "sub_1",
        "cycle_1",
    )

    assert payment is not None
    assert payment.id == "pay_latest_failed"
    assert payment.retry_scheduled_at == datetime(2026, 6, 11, tzinfo=UTC)


async def test_mongo_billing_retry_repository_rejects_stale_billing_result() -> None:
    due_at = datetime(2026, 6, 10, tzinfo=UTC)
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_due",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "cancel_scheduled",
                "cancel_at_period_end": True,
                "next_billing_at": None,
            }
        ]
    )
    payments = FakeCollection()
    invoices = FakeCollection()
    repository = MongoBillingRetryRepository(
        invoices=motor_collection_stub(invoices),
        payments=motor_collection_stub(payments),
        subscriptions=motor_collection_stub(subscriptions),
        subscription_plans=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )

    saved = await repository.save_subscription_billing_result(
        payment=Payment(
            id="pay_due",
            order_id="ord_due",
            amount=9900,
            status="paid",
            created_at=due_at,
            subscription_id="sub_due",
        ),
        invoice=Invoice(
            id="inv_due",
            user_id="user_1",
            payment_id="pay_due",
            status="paid",
            issued_at=due_at,
            subscription_id="sub_due",
        ),
        subscription=Subscription(
            id="sub_due",
            user_id="user_1",
            payment_customer_id="pcus_1",
            plan_id="plan_basic_monthly",
            product_code="basic",
            status="active",
            cancel_at_period_end=False,
            next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
        expected_next_billing_at=due_at,
    )

    assert saved is False
    assert subscriptions.documents["sub_due"]["status"] == "cancel_scheduled"
    assert payments.documents == {}
    assert invoices.documents == {}


async def test_mongo_billing_retry_repository_omits_empty_invoice_fields() -> None:
    due_at = datetime(2026, 6, 10, tzinfo=UTC)
    next_due_at = datetime(2026, 7, 10, tzinfo=UTC)
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_due",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
                "next_billing_at": due_at,
            }
        ]
    )
    invoices = FakeCollection()
    repository = MongoBillingRetryRepository(
        invoices=motor_collection_stub(invoices),
        payments=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(subscriptions),
        subscription_plans=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
        payment_instruments=motor_collection_stub(FakeCollection()),
    )

    saved = await repository.save_subscription_billing_result(
        payment=Payment(
            id="pay_due",
            order_id="ord_due",
            amount=9900,
            status="failed",
            created_at=due_at,
            subscription_id="sub_due",
            billing_cycle_key="cycle_1",
        ),
        invoice=Invoice(
            id="inv_due",
            user_id="user_1",
            payment_id="pay_due",
            status="issued",
            issued_at=due_at,
            subscription_id="sub_due",
            billing_cycle_key="cycle_1",
        ),
        subscription=Subscription(
            id="sub_due",
            user_id="user_1",
            payment_customer_id="pcus_1",
            plan_id="plan_basic_monthly",
            product_code="basic",
            status="active",
            cancel_at_period_end=False,
            next_billing_at=next_due_at,
        ),
        expected_next_billing_at=due_at,
    )

    assert saved is True
    assert invoices.documents["inv_due"] == {
        "_id": "inv_due",
        "user_id": "user_1",
        "payment_id": "pay_due",
        "status": "issued",
        "issued_at": due_at,
        "subscription_id": "sub_due",
        "billing_cycle_key": "cycle_1",
    }


async def test_mongo_subscription_billing_uow_wraps_billing_result() -> None:
    database = FakeDatabase()
    due_at = datetime(2026, 6, 10, tzinfo=UTC)
    next_due_at = datetime(2026, 7, 10, tzinfo=UTC)
    database.subscriptions = FakeCollection(
        [
            {
                "_id": "sub_due",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
                "next_billing_at": due_at,
            }
        ]
    )

    async with MongoSubscriptionBillingUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        saved = await uow.billing.save_subscription_billing_result(
            payment=Payment(
                id="pay_due",
                order_id="ord_due",
                amount=9900,
                status="paid",
                created_at=due_at,
                subscription_id="sub_due",
                billing_cycle_key="cycle_1",
            ),
            invoice=Invoice(
                id="inv_due",
                user_id="user_1",
                payment_id="pay_due",
                status="paid",
                issued_at=due_at,
                subscription_id="sub_due",
                billing_cycle_key="cycle_1",
            ),
            subscription=Subscription(
                id="sub_due",
                user_id="user_1",
                payment_customer_id="pcus_1",
                plan_id="plan_basic_monthly",
                product_code="basic",
                status="active",
                cancel_at_period_end=False,
                next_billing_at=next_due_at,
            ),
            expected_next_billing_at=due_at,
        )

    [session] = database.client.sessions
    assert saved is True
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.payments.documents["pay_due"]["status"] == "paid"
    assert database.invoices.documents["inv_due"]["status"] == "paid"
    assert database.subscriptions.documents["sub_due"]["next_billing_at"] == (
        next_due_at
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.subscriptions.calls
        if method == "replace_one"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.payments.calls
        if method == "replace_one"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.invoices.calls
        if method == "replace_one"
    )


async def test_mongo_subscription_cancel_uow_wraps_cancel_audit() -> None:
    database = FakeDatabase()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    period_end = datetime(2026, 7, 8, tzinfo=UTC)
    database.subscriptions = FakeCollection(
        [
            {
                "_id": "sub_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "active",
                "cancel_at_period_end": False,
                "current_period_start_at": datetime(2026, 6, 8, tzinfo=UTC),
                "current_period_end_at": period_end,
                "next_billing_at": period_end,
            }
        ]
    )

    async with MongoSubscriptionCancelUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        updated = await uow.subscriptions.schedule_subscription_cancel_at_period_end(
            "sub_123",
            "user_1",
            now,
        )
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id="idem_sub_cancel",
                scope="subscriptions-cancel",
                key_hash="key_hash",
                request_hash="request_hash",
                status="succeeded",
                created_at=now,
                updated_at=now,
                expires_at=period_end,
                resource_type="subscription",
                resource_id="sub_123",
            )
        )
        await uow.operator_audits.save_operator_audit(
            OperatorAudit(
                id="oaudit_sub_cancel",
                operator_id="user_1",
                action="subscription.cancel_scheduled",
                target_type="subscription",
                target_id="sub_123",
                previous_state={"status": "active"},
                next_state={"status": "cancel_scheduled"},
                reason_code="user_request",
                result="succeeded",
                created_at=now,
            )
        )

    [session] = database.client.sessions
    assert updated.status == "cancel_scheduled"
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.subscriptions.documents["sub_123"]["status"] == (
        "cancel_scheduled"
    )
    assert "next_billing_at" not in database.subscriptions.documents["sub_123"]
    assert database.idempotency_keys.documents["idem_sub_cancel"]["scope"] == (
        "subscriptions-cancel"
    )
    assert database.operator_audits.documents["oaudit_sub_cancel"]["action"] == (
        "subscription.cancel_scheduled"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.subscriptions.calls
        if method == "update_one"
    )


async def test_mongo_subscription_resume_uow_wraps_resume_audit() -> None:
    database = FakeDatabase()
    now = datetime(2026, 6, 11, tzinfo=UTC)
    period_end = datetime(2026, 7, 8, tzinfo=UTC)
    database.subscriptions = FakeCollection(
        [
            {
                "_id": "sub_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "cancel_scheduled",
                "cancel_at_period_end": True,
                "cancel_at": period_end,
                "access_until": period_end,
                "current_period_start_at": datetime(2026, 6, 8, tzinfo=UTC),
                "current_period_end_at": period_end,
            }
        ]
    )

    async with MongoSubscriptionResumeUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        updated = await uow.subscriptions.resume_cancel_scheduled_subscription(
            "sub_123",
            "user_1",
            now,
        )
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id="idem_sub_resume",
                scope="subscriptions-resume",
                key_hash="key_hash",
                request_hash="request_hash",
                status="succeeded",
                created_at=now,
                updated_at=now,
                expires_at=period_end,
                resource_type="subscription",
                resource_id="sub_123",
            )
        )
        await uow.operator_audits.save_operator_audit(
            OperatorAudit(
                id="oaudit_sub_resume",
                operator_id="user_1",
                action="subscription.resume",
                target_type="subscription",
                target_id="sub_123",
                previous_state={"status": "cancel_scheduled"},
                next_state={"status": "active"},
                reason_code="changed_mind",
                result="succeeded",
                created_at=now,
            )
        )

    [session] = database.client.sessions
    assert updated.status == "active"
    assert updated.cancel_at is None
    assert updated.next_billing_at == period_end
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.subscriptions.documents["sub_123"]["status"] == "active"
    assert database.subscriptions.documents["sub_123"]["next_billing_at"] == (
        period_end
    )
    assert "cancel_at" not in database.subscriptions.documents["sub_123"]
    assert "access_until" not in database.subscriptions.documents["sub_123"]
    assert database.idempotency_keys.documents["idem_sub_resume"]["scope"] == (
        "subscriptions-resume"
    )
    assert database.operator_audits.documents["oaudit_sub_resume"]["action"] == (
        "subscription.resume"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.subscriptions.calls
        if method == "update_one"
    )


async def test_mongo_subscription_resume_rejects_elapsed_period() -> None:
    now = datetime(2026, 7, 8, tzinfo=UTC)
    period_end = datetime(2026, 7, 8, tzinfo=UTC)
    subscriptions = FakeCollection(
        [
            {
                "_id": "sub_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "cancel_scheduled",
                "cancel_at_period_end": True,
                "cancel_at": period_end,
                "access_until": period_end,
                "current_period_start_at": datetime(2026, 6, 8, tzinfo=UTC),
                "current_period_end_at": period_end,
            }
        ]
    )
    repository = MongoSubscriptionAccountRepository(
        subscriptions=motor_collection_stub(subscriptions),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
        billing_methods=motor_collection_stub(FakeCollection()),
    )

    with pytest.raises(LookupError, match="not resumable"):
        await repository.resume_cancel_scheduled_subscription(
            "sub_123",
            "user_1",
            now,
        )

    assert subscriptions.documents["sub_123"]["status"] == "cancel_scheduled"
    assert "next_billing_at" not in subscriptions.documents["sub_123"]


async def test_mongo_subscription_expiration_uow_wraps_expiration_audit() -> None:
    database = FakeDatabase()
    now = datetime(2026, 7, 8, tzinfo=UTC)
    period_end = datetime(2026, 7, 8, tzinfo=UTC)
    database.subscriptions = FakeCollection(
        [
            {
                "_id": "sub_123",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "plan_id": "plan_basic_monthly",
                "product_code": "basic",
                "status": "cancel_scheduled",
                "cancel_at_period_end": True,
                "cancel_at": period_end,
                "access_until": period_end,
                "current_period_start_at": datetime(2026, 6, 8, tzinfo=UTC),
                "current_period_end_at": period_end,
            }
        ]
    )

    async with MongoSubscriptionExpirationUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        changed = await uow.subscriptions.expire_cancel_scheduled_subscription(
            "sub_123",
            now,
        )
        await uow.operator_audits.save_operator_audit(
            OperatorAudit(
                id="oaudit_sub_expire",
                operator_id="system:subscription-expiration",
                action="subscription.cancel_expired",
                target_type="subscription",
                target_id="sub_123",
                previous_state={"status": "cancel_scheduled"},
                next_state={"status": "canceled"},
                reason_code="cancel_at_period_end_elapsed",
                result="succeeded",
                created_at=now,
            )
        )

    [session] = database.client.sessions
    assert changed is True
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.subscriptions.documents["sub_123"]["status"] == "canceled"
    assert database.subscriptions.documents["sub_123"]["canceled_at"] == now
    assert database.subscriptions.documents["sub_123"]["access_until"] == period_end
    assert database.subscriptions.documents["sub_123"]["next_billing_at"] is None
    assert database.operator_audits.documents["oaudit_sub_expire"]["action"] == (
        "subscription.cancel_expired"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.subscriptions.calls
        if method == "update_one"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.operator_audits.calls
        if method == "replace_one"
    )


async def test_mongo_subscription_change_uow_wraps_change_audit() -> None:
    database = FakeDatabase()
    now = datetime(2026, 6, 11, tzinfo=UTC)
    period_end = datetime(2026, 7, 8, tzinfo=UTC)
    subscription = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        current_period_start_at=datetime(2026, 6, 8, tzinfo=UTC),
        current_period_end_at=period_end,
        next_billing_at=period_end,
        pending_plan_id="plan_pro_monthly",
        pending_plan_effective_at=period_end,
    )

    async with MongoSubscriptionChangeUnitOfWorkFactory(
        motor_database_stub(database)
    )() as uow:
        await uow.billing.save_payment(
            Payment(
                id="pay_plan_change",
                order_id="ord_plan_change",
                amount=5000,
                status="paid",
                created_at=now,
                subscription_id="sub_123",
                billing_cycle_key="cycle_plan_change",
            )
        )
        await uow.billing.save_invoice(
            Invoice(
                id="inv_plan_change",
                user_id="user_1",
                payment_id="pay_plan_change",
                status="paid",
                issued_at=now,
                subscription_id="sub_123",
                billing_cycle_key="cycle_plan_change",
            )
        )
        await uow.subscriptions.save_subscription(subscription)
        await uow.idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id="idem_sub_change",
                scope="subscriptions-change",
                key_hash="key_hash",
                request_hash="request_hash",
                status="succeeded",
                created_at=now,
                updated_at=now,
                expires_at=period_end,
                resource_type="subscription",
                resource_id="sub_123",
            )
        )
        await uow.operator_audits.save_operator_audit(
            OperatorAudit(
                id="oaudit_sub_change",
                operator_id="user_1",
                action="subscription.plan_change",
                target_type="subscription",
                target_id="sub_123",
                previous_state={"plan_id": "plan_basic_monthly"},
                next_state={"pending_plan_id": "plan_pro_monthly"},
                reason_code="user_request",
                result="succeeded",
                created_at=now,
            )
        )

    [session] = database.client.sessions
    assert session.started is True
    assert session.committed is True
    assert session.aborted is False
    assert session.ended is True
    assert database.subscriptions.documents["sub_123"]["pending_plan_id"] == (
        "plan_pro_monthly"
    )
    assert database.payments.documents["pay_plan_change"]["status"] == "paid"
    assert database.invoices.documents["inv_plan_change"]["status"] == "paid"
    assert database.idempotency_keys.documents["idem_sub_change"]["scope"] == (
        "subscriptions-change"
    )
    assert database.operator_audits.documents["oaudit_sub_change"]["action"] == (
        "subscription.plan_change"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.subscriptions.calls
        if method == "replace_one"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.payments.calls
        if method == "replace_one"
    )
    assert any(
        kwargs.get("session") is session
        for method, kwargs in database.invoices.calls
        if method == "replace_one"
    )


async def test_mongo_billing_method_repository_deactivates_method() -> None:
    billing_methods = FakeCollection(
        [
            {
                "_id": "bm_456",
                "user_id": "user_1",
                "payment_customer_id": "pcus_1",
                "instrument_id": "pinstr_2",
                "display_name": "신한카드 **** 5678",
                "provider": "tosspayments",
                "is_default": False,
                "status": "active",
                "method": "카드",
                "card_company": "신한",
                "masked_number": "**** **** **** 5678",
                "billing_key_status": "active",
                "created_at": datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
            }
        ]
    )
    repository = MongoBillingMethodRepository(
        billing_methods=motor_collection_stub(billing_methods),
        payment_instruments=motor_collection_stub(
            FakeCollection([{"_id": "pinstr_2", "status": "active"}])
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
    )
    deleted_at = datetime(2026, 6, 10, tzinfo=UTC)

    await repository.deactivate_billing_method_for_user(
        "bm_456",
        "user_1",
        deleted_at,
    )

    assert billing_methods.documents["bm_456"]["status"] == "inactive"
    assert billing_methods.documents["bm_456"]["is_default"] is False
    assert billing_methods.documents["bm_456"]["deleted_at"] == deleted_at
    assert "billing_key_status" not in billing_methods.documents["bm_456"]


async def test_mongo_invoice_repository_loads_user_invoice_summary_and_detail() -> (
    None
):
    issued_at = datetime(2026, 7, 8, 9, 0, tzinfo=UTC)
    repository = MongoInvoiceRepository(
        invoices=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "inv_123",
                        "user_id": "user_1",
                        "payment_id": "pay_123",
                        "subscription_id": "sub_123",
                        "status": "issued",
                        "amount": 9900,
                        "currency": "KRW",
                        "issued_at": issued_at,
                    },
                    {
                        "_id": "inv_old",
                        "user_id": "user_1",
                        "payment_id": "pay_old",
                        "subscription_id": "sub_123",
                        "status": "paid",
                        "amount": 9900,
                        "currency": "KRW",
                        "issued_at": datetime(2026, 6, 8, 9, 0, tzinfo=UTC),
                    }
                ]
            )
        ),
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_123",
                        "status": "failed",
                        "amount": 9900,
                        "failure": {
                            "phase": "confirm",
                            "reason": "provider_rejected",
                            "providerCode": "INSUFFICIENT_FUNDS",
                            "message": "잔액 부족",
                            "retryable": True,
                        },
                        "retry_scheduled_at": datetime(2026, 7, 10, tzinfo=UTC),
                    }
                ]
            )
        ),
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_123",
                        "plan_id": "plan_basic_monthly",
                        "status": "canceled",
                    }
                ]
            )
        ),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_basic_monthly",
                        "product_id": "product_basic",
                        "billing_period": "monthly",
                        "name": "월간 Pro",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "name": "Analytics Pro",
                    }
                ]
            )
        ),
    )

    rows = await repository.list_invoices_for_user(
        "user_1",
        20,
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 31),
    )
    detail = await repository.get_invoice_detail_for_user("inv_123", "user_1")
    owner_id = await repository.get_invoice_owner("inv_123")

    assert len(rows) == 1
    assert rows[0].invoice_id == "inv_123"
    assert rows[0].billing_date == date(2026, 7, 8)
    assert rows[0].product_name == "Analytics Pro"
    assert rows[0].plan_name == "월간 Pro"
    assert rows[0].payment_status == "failed"
    assert rows[0].failure_summary == "잔액 부족"
    assert detail is not None
    assert detail.subscription_status == "canceled"
    assert detail.failure_code == "INSUFFICIENT_FUNDS"
    assert detail.retry_available is True
    assert owner_id == "user_1"


async def test_mongo_invoice_repository_uses_latest_failed_payment_for_invoice() -> (
    None
):
    billing_cycle_key = "sub_123:2026-07-08T00:00:00+00:00"
    repository = MongoInvoiceRepository(
        invoices=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "inv_retry",
                        "user_id": "user_1",
                        "payment_id": "pay_original_failed",
                        "subscription_id": "sub_123",
                        "billing_cycle_key": billing_cycle_key,
                        "status": "issued",
                        "amount": 9900,
                        "currency": "KRW",
                        "issued_at": datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
                    },
                ]
            )
        ),
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_retry_failed",
                        "subscription_id": "sub_123",
                        "billing_cycle_key": billing_cycle_key,
                        "status": "failed",
                        "amount": 9900,
                        "created_at": datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
                        "failure": {
                            "reason": "billing_key_invalid",
                            "providerCode": "INVALID_BILLING_KEY",
                            "message": "최신 재시도 실패",
                            "retryable": True,
                        },
                        "retry_scheduled_at": datetime(2026, 7, 10, tzinfo=UTC),
                    },
                    {
                        "_id": "pay_original_failed",
                        "subscription_id": "sub_123",
                        "billing_cycle_key": billing_cycle_key,
                        "status": "failed",
                        "amount": 9900,
                        "created_at": datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
                        "failure": {
                            "reason": "provider_rejected",
                            "providerCode": "INSUFFICIENT_FUNDS",
                            "message": "초기 결제 실패",
                            "retryable": True,
                        },
                        "retry_scheduled_at": datetime(2026, 7, 9, tzinfo=UTC),
                    },
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_invoices_for_user("user_1", 20)
    detail = await repository.get_invoice_detail_for_user("inv_retry", "user_1")

    assert rows[0].payment_status == "failed"
    assert rows[0].failure_summary == "최신 재시도 실패"
    assert detail is not None
    assert detail.failure_code == "INVALID_BILLING_KEY"
    assert detail.retry_scheduled_at == datetime(2026, 7, 10, tzinfo=UTC)


async def test_mongo_invoice_repository_falls_back_to_catalog_plan_display_name() -> (
    None
):
    repository = MongoInvoiceRepository(
        invoices=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "inv_fallback",
                        "user_id": "user_1",
                        "payment_id": "pay_fallback",
                        "subscription_id": "sub_fallback",
                        "status": "paid",
                        "amount": 9900,
                        "currency": "KRW",
                        "issued_at": datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
                    },
                ]
            )
        ),
        payments=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "sub_fallback",
                        "plan_id": "plan_fallback",
                        "status": "active",
                    }
                ]
            )
        ),
        subscription_plans=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "plan_fallback",
                        "product_id": "product_basic",
                        "billing_period": "monthly",
                    }
                ]
            )
        ),
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_basic",
                        "name": "Basic",
                    }
                ]
            )
        ),
    )

    rows = await repository.list_invoices_for_user("user_1", 20)

    assert rows[0].product_name == "Basic"
    assert rows[0].plan_name == "Basic 월간"


async def test_mongo_invoice_repository_filters_by_payment_status() -> None:
    repository = MongoInvoiceRepository(
        invoices=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "inv_failed",
                        "user_id": "user_1",
                        "payment_id": "pay_failed",
                        "subscription_id": "sub_123",
                        "status": "issued",
                        "amount": 9900,
                        "currency": "KRW",
                        "issued_at": datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
                    },
                    {
                        "_id": "inv_paid",
                        "user_id": "user_1",
                        "payment_id": "pay_paid",
                        "subscription_id": "sub_123",
                        "status": "paid",
                        "amount": 9900,
                        "currency": "KRW",
                        "issued_at": datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
                    },
                ]
            )
        ),
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {"_id": "pay_failed", "status": "failed", "amount": 9900},
                    {"_id": "pay_paid", "status": "paid", "amount": 9900},
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
    )

    rows = await repository.list_invoices_for_user(
        "user_1",
        20,
        payment_status="failed",
    )

    assert [row.invoice_id for row in rows] == ["inv_failed"]
    assert rows[0].payment_status == "failed"


async def test_mongo_invoice_detail_ignores_retry_schedule_for_non_failed_payment() -> (
    None
):
    repository = MongoInvoiceRepository(
        invoices=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "inv_paid_retry_stale",
                        "user_id": "user_1",
                        "payment_id": "pay_paid_retry_stale",
                        "subscription_id": "sub_123",
                        "status": "paid",
                        "amount": 9900,
                        "currency": "KRW",
                        "issued_at": datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
                    },
                ]
            )
        ),
        payments=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "pay_paid_retry_stale",
                        "status": "paid",
                        "amount": 9900,
                        "retry_scheduled_at": datetime(2026, 7, 10, tzinfo=UTC),
                    },
                ]
            )
        ),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
    )

    detail = await repository.get_invoice_detail_for_user(
        "inv_paid_retry_stale",
        "user_1",
    )

    assert detail is not None
    assert detail.payment_status == "paid"
    assert detail.retry_available is False
    assert detail.retry_scheduled_at is None


async def test_mongo_invoice_repository_saves_invoice_document() -> None:
    invoices = FakeCollection()
    repository = MongoInvoiceRepository(
        invoices=motor_collection_stub(invoices),
        payments=motor_collection_stub(FakeCollection()),
        subscriptions=motor_collection_stub(FakeCollection()),
        subscription_plans=motor_collection_stub(FakeCollection()),
        products=motor_collection_stub(FakeCollection()),
    )

    await repository.save_invoice(
        Invoice(
            id="inv_123",
            user_id="user_1",
            payment_id="pay_123",
            status="paid",
            issued_at=datetime(2026, 6, 10, tzinfo=UTC),
            receipt_url="https://receipt.example",
        )
    )

    assert invoices.documents["inv_123"]["user_id"] == "user_1"
    assert invoices.documents["inv_123"]["payment_id"] == "pay_123"
    assert invoices.documents["inv_123"]["status"] == "paid"
    assert invoices.documents["inv_123"]["receipt_url"] == "https://receipt.example"
    assert "subscription_id" not in invoices.documents["inv_123"]


async def test_mongo_one_time_sku_repository_reserves_limited_stock() -> None:
    one_time_sku_documents = FakeCollection(
        [
            {
                "_id": "sku_limited",
                "product_id": "product_reports",
                "sku_code": "LIMITED",
                "amount": 25000,
                "stock_policy": "limited",
                "total_stock": 5,
                "reserved_stock": 1,
                "sold_stock": 1,
                "status": "active",
            }
        ]
    )
    one_time_skus = MongoOneTimeSkuRepository(
        products=motor_collection_stub(
            FakeCollection(
                [
                    {
                        "_id": "product_reports",
                        "product_code": "reports",
                        "product_type": "one_time",
                        "name": "Reports",
                        "status": "active",
                    }
                ]
            )
        ),
        one_time_skus=motor_collection_stub(one_time_sku_documents),
    )
    sku = OneTimeSku(
        id="sku_limited",
        product_id="product_reports",
        sku_code="LIMITED",
        amount=25000,
        stock_policy="limited",
        total_stock=5,
        reserved_stock=1,
        sold_stock=1,
        status="active",
    )

    assert await one_time_skus.reserve_one_time_sku_stock(sku, 3)
    assert one_time_sku_documents.documents["sku_limited"]["reserved_stock"] == 4
    assert not await one_time_skus.reserve_one_time_sku_stock(sku, 1)


async def test_mongo_one_time_sku_repository_restores_sold_stock() -> None:
    one_time_sku_documents = FakeCollection(
        [
            {
                "_id": "sku_limited",
                "product_id": "product_reports",
                "sku_code": "LIMITED",
                "amount": 25000,
                "stock_policy": "limited",
                "total_stock": 5,
                "reserved_stock": 0,
                "sold_stock": 2,
                "status": "active",
            }
        ]
    )
    one_time_skus = MongoOneTimeSkuRepository(
        products=motor_collection_stub(FakeCollection()),
        one_time_skus=motor_collection_stub(one_time_sku_documents),
    )

    await one_time_skus.restore_sold_one_time_sku_stock("sku_limited", 2)
    await one_time_skus.restore_sold_one_time_sku_stock("sku_limited", 1)

    assert one_time_sku_documents.documents["sku_limited"]["sold_stock"] == 0
