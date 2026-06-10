from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from payments.adapters.mongo.catalog import MongoCatalogRepository
from payments.adapters.mongo.indexes import ensure_mongo_indexes
from payments.adapters.mongo.payments import MongoPaymentRepository
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.payment import Payment

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


class FakeCollection:
    def __init__(self, documents=None) -> None:
        self.documents = {document["_id"]: document for document in documents or []}
        self.indexes = []

    async def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def find(self, query):
        return FakeCursor(
            document
            for document in self.documents.values()
            if all(document.get(key) == value for key, value in query.items())
        )

    async def find_one(self, query):
        for document in self.documents.values():
            if all(document.get(key) == value for key, value in query.items()):
                return document
        return None

    async def replace_one(self, query, document, upsert=False):
        document_id = query["_id"]
        if upsert or document_id in self.documents:
            self.documents[document_id] = document


class FakeDatabase:
    def __init__(self) -> None:
        self.products = FakeCollection()
        self.subscription_plans = FakeCollection()
        self.checkouts = FakeCollection()
        self.payments = FakeCollection()
        self.idempotency_keys = FakeCollection()


def motor_collection_stub(
    collection: FakeCollection,
) -> AsyncIOMotorCollection[TestMongoDocument]:
    return cast(AsyncIOMotorCollection[TestMongoDocument], collection)


def motor_database_stub(
    database: FakeDatabase,
) -> AsyncIOMotorDatabase[TestMongoDocument]:
    return cast(AsyncIOMotorDatabase[TestMongoDocument], database)


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
        index[1]["name"] == "ttl_idempotency_expires_at"
        for index in database.idempotency_keys.indexes
    )


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
    ).list_active_subscription_catalog()

    assert len(rows) == 1
    assert rows[0][0].id == "product_basic"
    assert rows[0][1].id == "plan_basic_monthly"


async def test_mongo_payment_repository_enforces_checkout_ownership() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    repository = MongoPaymentRepository(
        checkouts=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(FakeCollection()),
        idempotency_keys=motor_collection_stub(FakeCollection()),
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

    await repository.save_checkout(checkout)
    await repository.save_payment(payment)

    assert await repository.get_payment_for_user("pay_1", "user_1") == payment
    assert await repository.get_payment_for_user("pay_1", "user_2") is None


async def test_mongo_payment_repository_looks_up_idempotency_by_scope_and_hash() -> (
    None
):
    now = datetime(2026, 6, 10, tzinfo=UTC)
    repository = MongoPaymentRepository(
        checkouts=motor_collection_stub(FakeCollection()),
        payments=motor_collection_stub(FakeCollection()),
        idempotency_keys=motor_collection_stub(FakeCollection()),
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

    await repository.save_idempotency_key(key)

    assert await repository.find_idempotency_key("payments-orders", "hash") == key
    assert await repository.find_idempotency_key("other", "hash") is None
