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
    await database.one_time_skus.create_index(
        [("product_id", ASCENDING), ("sku_code", ASCENDING)],
        unique=True,
        name="uniq_one_time_skus_product_sku_code",
    )
    await database.one_time_skus.create_index(
        [("product_id", ASCENDING), ("status", ASCENDING), ("stock_policy", ASCENDING)],
        name="idx_one_time_skus_product_status_stock_policy",
    )
    await database.checkouts.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        name="idx_checkouts_user_created_at",
    )
    await database.payment_customers.create_index(
        [("user_id", ASCENDING), ("provider", ASCENDING)],
        unique=True,
        name="uniq_payment_customers_user_provider",
    )
    await database.payment_customers.create_index(
        [("provider", ASCENDING), ("customer_key", ASCENDING)],
        unique=True,
        name="uniq_payment_customers_provider_customer_key",
    )
    await database.payment_cancel_requests.create_index(
        [("payment_id", ASCENDING), ("idempotency_key_hash", ASCENDING)],
        unique=True,
        name="uniq_payment_cancel_requests_idempotency",
    )
    await database.payment_cancel_requests.create_index(
        [("status", ASCENDING), ("created_at", ASCENDING)],
        name="idx_payment_cancel_requests_pending_created_at",
        partialFilterExpression={"status": "pending"},
    )
    await database.billing_auths.create_index(
        [("user_id", ASCENDING), ("status", ASCENDING)],
        name="idx_billing_auths_user_status",
    )
    await database.operation_locks.create_index(
        [("lock_key", ASCENDING)],
        unique=True,
        name="uniq_operation_locks_lock_key",
    )
    await database.operation_locks.create_index(
        [("status", ASCENDING), ("locked_until_at", ASCENDING)],
        name="idx_operation_locks_status_until",
    )
    await database.operation_locks.create_index(
        [("locked_until_at", ASCENDING)],
        expireAfterSeconds=0,
        name="ttl_operation_locks_locked_until_at",
    )
    await database.operator_audits.create_index(
        [
            ("target_type", ASCENDING),
            ("target_id", ASCENDING),
            ("created_at", ASCENDING),
        ],
        name="idx_operator_audits_target",
    )
    await database.operator_audits.create_index(
        [("operator_id", ASCENDING), ("created_at", ASCENDING)],
        name="idx_operator_audits_operator",
    )
    await database.operator_audits.create_index(
        [("action", ASCENDING), ("created_at", ASCENDING)],
        name="idx_operator_audits_action",
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
    await database.payments.create_index(
        [("subscription_id", ASCENDING), ("created_at", ASCENDING)],
        name="idx_payments_subscription_created_at",
    )
    await database.payments.create_index(
        [("status", ASCENDING), ("expires_at", ASCENDING)],
        name="idx_payments_ready_expires_at",
        partialFilterExpression={
            "status": "ready",
            "expires_at": {"$type": "date"},
        },
    )
    await database.payments.create_index(
        [("status", ASCENDING), ("retry_scheduled_at", ASCENDING)],
        name="idx_payments_failed_retry_scheduled_at",
        partialFilterExpression={
            "status": "failed",
            "retry_scheduled_at": {"$type": "date"},
        },
    )
    await database.payments.create_index(
        [("payment_key", ASCENDING)],
        unique=True,
        sparse=True,
        name="uniq_payments_payment_key_sparse",
    )
    await database.payments.create_index(
        [("checkout_id", ASCENDING)],
        unique=True,
        name="uniq_payments_paid_checkout",
        partialFilterExpression={
            "checkout_id": {"$type": "string"},
            "status": "paid",
        },
    )
    await database.payments.create_index(
        [("subscription_id", ASCENDING), ("billing_cycle_key", ASCENDING)],
        unique=True,
        name="uniq_payments_subscription_billing_cycle_paid",
        partialFilterExpression={
            "subscription_id": {"$type": "string"},
            "billing_cycle_key": {"$type": "string"},
            "status": "paid",
        },
    )
    await database.invoices.create_index(
        [("user_id", ASCENDING), ("issued_at", ASCENDING)],
        name="idx_invoices_user_issued_at",
    )
    await database.invoices.create_index(
        [("subscription_id", ASCENDING), ("billing_cycle_key", ASCENDING)],
        unique=True,
        name="uniq_invoices_subscription_billing_cycle",
        partialFilterExpression={
            "subscription_id": {"$type": "string"},
            "billing_cycle_key": {"$type": "string"},
            "status": {"$in": ["issued", "paid"]},
        },
    )
    await database.idempotency_keys.create_index(
        [("scope", ASCENDING), ("key_hash", ASCENDING)],
        unique=True,
        name="uniq_idempotency_keys_scope_key",
    )
    await database.idempotency_keys.create_index(
        [("resource_type", ASCENDING), ("resource_id", ASCENDING)],
        name="idx_idempotency_keys_resource",
    )
    await database.idempotency_keys.create_index(
        [("expires_at", ASCENDING)],
        expireAfterSeconds=0,
        name="ttl_idempotency_keys_expires_at",
    )
    await database.subscriptions.create_index(
        [("user_id", ASCENDING), ("status", ASCENDING)],
        name="idx_subscriptions_user_status",
    )
    await database.subscriptions.create_index(
        [("next_billing_at", ASCENDING), ("status", ASCENDING)],
        name="idx_subscriptions_next_billing_status",
    )
    await database.subscriptions.create_index(
        [("status", ASCENDING), ("current_period_end_at", ASCENDING)],
        name="idx_subscriptions_cancel_expiration",
        partialFilterExpression={"status": "cancel_scheduled"},
    )
    await database.subscriptions.create_index(
        [("user_id", ASCENDING), ("product_code", ASCENDING)],
        unique=True,
        name="uniq_subscriptions_user_product_service_holding",
        partialFilterExpression={
            "status": {
                "$in": ["pending", "active", "past_due", "cancel_scheduled"]
            }
        },
    )
    await database.billing_methods.create_index(
        [("user_id", ASCENDING), ("is_default", ASCENDING), ("status", ASCENDING)],
        name="idx_billing_methods_user_default_status",
    )
    await database.billing_methods.create_index(
        [("user_id", ASCENDING), ("is_default", ASCENDING)],
        unique=True,
        name="uniq_billing_methods_active_default",
        partialFilterExpression={"is_default": True, "status": "active"},
    )
    await database.payment_instruments.create_index(
        [("payment_customer_id", ASCENDING), ("status", ASCENDING)],
        name="idx_payment_instruments_customer_status",
    )
    await database.payment_instruments.create_index(
        [("provider", ASCENDING), ("billing_key_hash", ASCENDING)],
        unique=True,
        name="uniq_payment_instruments_provider_billing_key_hash",
    )
    await database.admin_accounts.create_index(
        [("email_lower", ASCENDING)],
        unique=True,
        name="uniq_admin_accounts_email_lower",
    )
    await database.admin_accounts.create_index(
        [("status", ASCENDING)],
        name="idx_admin_accounts_status",
    )
    await database.admin_auth_tokens.create_index(
        [("token_hash", ASCENDING)],
        unique=True,
        name="uniq_admin_auth_tokens_hash",
    )
    await database.admin_auth_tokens.create_index(
        [
            ("admin_account_id", ASCENDING),
            ("token_type", ASCENDING),
            ("status", ASCENDING),
        ],
        name="idx_admin_auth_tokens_account_type_status",
    )
    await database.admin_auth_tokens.create_index(
        [("expires_at", ASCENDING)],
        expireAfterSeconds=0,
        name="ttl_admin_auth_tokens_expires_at",
    )
    await database.webhook_events.create_index(
        [("provider", ASCENDING), ("event_id", ASCENDING)],
        unique=True,
        name="uniq_webhook_events_provider_event",
    )
