from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

ASCENDING = 1
DESCENDING = -1


async def ensure_mongo_indexes(database: AsyncIOMotorDatabase) -> None:
    await database.products.create_index(
        [("product_code", ASCENDING), ("product_type", ASCENDING)],
        unique=True,
        name="uniq_products_code_type",
    )
    await database.subscription_plans.create_index(
        [("product_id", ASCENDING), ("plan_code", ASCENDING)],
        unique=True,
        name="uniq_subscription_plans_product_plan_code",
    )
    await database.subscription_plans.create_index(
        [("product_id", ASCENDING), ("status", ASCENDING)],
        name="idx_subscription_plans_product_status",
    )
    await database.checkouts.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        name="idx_checkouts_user_created_at",
    )
    await database.payments.create_index(
        [("order_id", ASCENDING)],
        unique=True,
        name="uniq_payments_order_id",
    )
    await database.payments.create_index(
        [("checkout_id", ASCENDING)],
        name="idx_payments_checkout_id",
    )
    await database.idempotency_keys.create_index(
        [("scope", ASCENDING), ("key_hash", ASCENDING)],
        unique=True,
        name="uniq_idempotency_scope_key_hash",
    )
    await database.idempotency_keys.create_index(
        [("expires_at", ASCENDING)],
        expireAfterSeconds=0,
        name="ttl_idempotency_expires_at",
    )
