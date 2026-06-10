from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from payments.adapters.mongo.catalog import MongoCatalogRepository
from payments.adapters.mongo.checkouts import MongoCheckoutRepository
from payments.adapters.mongo.idempotency import MongoIdempotencyKeyRepository
from payments.adapters.mongo.indexes import ensure_mongo_indexes
from payments.adapters.mongo.one_time_skus import MongoOneTimeSkuRepository
from payments.adapters.mongo.payment_attempts import MongoPaymentAttemptRepository
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.one_time_sku import OneTimeSku
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

    async def find_one(self, query, **kwargs):
        for document in self.documents.values():
            if all(document.get(key) == value for key, value in query.items()):
                return document
        return None

    async def replace_one(self, query, document, upsert=False, **kwargs):
        document_id = query["_id"]
        if upsert or document_id in self.documents:
            self.documents[document_id] = document

    async def update_one(self, query, update, **kwargs):
        document = await self.find_one(
            {key: value for key, value in query.items() if key != "$expr"}
        )
        if document is None or not _matches_stock_expression(document, query):
            return FakeUpdateResult(modified_count=0)
        for key, value in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + value
        return FakeUpdateResult(modified_count=1)


class FakeUpdateResult:
    def __init__(self, modified_count: int) -> None:
        self.modified_count = modified_count


def _matches_stock_expression(document, query) -> bool:
    if "$expr" not in query:
        return True
    required_quantity = query["$expr"]["$gte"][1]
    available_stock = (
        document["total_stock"] - document["reserved_stock"] - document["sold_stock"]
    )
    return available_stock >= required_quantity


class FakeDatabase:
    def __init__(self) -> None:
        self.products = FakeCollection()
        self.subscription_plans = FakeCollection()
        self.one_time_skus = FakeCollection()
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
        index[1]["name"] == "idx_one_time_skus_product_status_stock_policy"
        for index in database.one_time_skus.indexes
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

    assert await payment_attempts.get_payment_for_user("pay_1", "user_1") == payment
    assert await payment_attempts.get_payment_for_user("pay_1", "user_2") is None


async def test_mongo_idempotency_repository_looks_up_by_scope_and_hash() -> (
    None
):
    now = datetime(2026, 6, 10, tzinfo=UTC)
    idempotency_keys = MongoIdempotencyKeyRepository(
        motor_collection_stub(FakeCollection())
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

    assert await idempotency_keys.find_idempotency_key("payments-orders", "hash") == key
    assert await idempotency_keys.find_idempotency_key("other", "hash") is None


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
        products=motor_collection_stub(FakeCollection()),
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
