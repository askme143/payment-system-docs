from __future__ import annotations

from typing import cast

import pytest
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorDatabase

from payments.adapters.crypto import FernetBillingKeyCipher
from payments.adapters.mongo import (
    MongoAdminAuthRepository,
    MongoAdminAuthUnitOfWorkFactory,
    MongoAdminCatalogRepository,
    MongoAdminOperationsRepository,
    MongoAdminSubscriptionAdjustUnitOfWorkFactory,
    MongoBillingAuthRepository,
    MongoBillingMethodDefaultUnitOfWorkFactory,
    MongoBillingMethodDeleteUnitOfWorkFactory,
    MongoBillingMethodRepository,
    MongoBillingRetryRepository,
    MongoCatalogRepository,
    MongoIdempotencyKeyRepository,
    MongoInvoiceRepository,
    MongoNotificationOutboxRepository,
    MongoNotificationTemplateRepository,
    MongoOneTimePaymentUnitOfWorkFactory,
    MongoOperationLockRepository,
    MongoPaymentAttemptRepository,
    MongoPaymentCustomerRepository,
    MongoSubscriptionAccountRepository,
    MongoSubscriptionBillingUnitOfWorkFactory,
    MongoSubscriptionCancelUnitOfWorkFactory,
    MongoSubscriptionChangeUnitOfWorkFactory,
    MongoSubscriptionCheckoutRepository,
    MongoSubscriptionConfirmUnitOfWorkFactory,
    MongoSubscriptionExpirationRepository,
    MongoSubscriptionExpirationUnitOfWorkFactory,
    MongoSubscriptionResumeUnitOfWorkFactory,
    MongoWebhookRepository,
    MongoWebhookUnitOfWorkFactory,
)
from payments.adapters.notifications import AdminAuthOutboxEmailSender
from payments.adapters.rate_limit import InMemoryAdminAuthRateLimiter
from payments.adapters.subscription_change_tokens import (
    HmacSubscriptionChangeTokenCodec,
)
from payments.adapters.time import SystemClock
from payments.adapters.toss import TossPaymentProvider
from payments.http.composition import build_http_dependencies, create_app
from payments.http.config import PaymentHttpConfig, payment_config_from_env

TestMongoDocument = dict[str, object]


class FakeDatabase:
    admin_accounts = object()
    admin_auth_tokens = object()
    products = object()
    subscription_plans = object()
    one_time_skus = object()
    checkouts = object()
    payments = object()
    idempotency_keys = object()
    subscriptions = object()
    billing_methods = object()
    payment_instruments = object()
    invoices = object()
    operator_audits = object()
    operation_locks = object()
    operation_lock_counters = object()
    billing_auths = object()
    payment_customers = object()
    webhook_events = object()
    notification_outbox = object()
    notification_templates = object()


def motor_database_stub() -> AsyncIOMotorDatabase[TestMongoDocument]:
    return cast(AsyncIOMotorDatabase[TestMongoDocument], FakeDatabase())


class TestHttpComposition:
    def test_payment_config_from_env_loads_required_values(self) -> None:
        config = payment_config_from_env(
            {
                "PAYMENTS_DATABASE_URL": "mongodb://localhost:27017",
                "PAYMENTS_DATABASE_NAME": "payments",
                "PAYMENTS_INTERNAL_SERVICE_TOKEN": "secret",
                "PAYMENTS_TOSS_WEBHOOK_SECRET": "webhook-secret",
                "PAYMENTS_BILLING_KEY_ENCRYPTION_SECRET": "billing-key-secret",
                "PAYMENTS_NOTIFICATION_TEMPLATE_ARG_ENCRYPTION_SECRET": (
                    "notification-secret"
                ),
                "PAYMENTS_ADMIN_AUTH_LINK_BASE_URL": "https://admin.example.com",
                "PAYMENTS_NOTIFICATION_RECIPIENT_API_BASE_URL": (
                    "https://member.example.com"
                ),
                "PAYMENTS_ALLOWED_REDIRECT_HOSTS": "example.com,app.example.com",
            }
        )

        assert config.database_url == "mongodb://localhost:27017"
        assert config.database_name == "payments"
        assert config.internal_service_token == "secret"
        assert config.toss_webhook_secret == "webhook-secret"
        assert config.billing_key_encryption_secret == "billing-key-secret"
        assert (
            config.notification_template_arg_encryption_secret
            == "notification-secret"
        )
        assert config.admin_auth_link_base_url == "https://admin.example.com"
        assert (
            config.notification_recipient_api_base_url
            == "https://member.example.com"
        )
        assert config.allowed_redirect_hosts == ("example.com", "app.example.com")

    def test_payment_config_from_env_rejects_missing_required_values(self) -> None:
        with pytest.raises(ValueError, match="PAYMENTS_DATABASE_URL"):
            payment_config_from_env({})
        with pytest.raises(ValueError, match="PAYMENTS_TOSS_WEBHOOK_SECRET"):
            payment_config_from_env(
                {
                    "PAYMENTS_DATABASE_URL": "mongodb://localhost:27017",
                    "PAYMENTS_DATABASE_NAME": "payments",
                    "PAYMENTS_INTERNAL_SERVICE_TOKEN": "secret",
                    "PAYMENTS_ADMIN_AUTH_LINK_BASE_URL": "https://admin.example.com",
                    "PAYMENTS_NOTIFICATION_RECIPIENT_API_BASE_URL": (
                        "https://member.example.com"
                    ),
                }
            )
        with pytest.raises(ValueError, match="PAYMENTS_ADMIN_AUTH_LINK_BASE_URL"):
            payment_config_from_env(
                {
                    "PAYMENTS_DATABASE_URL": "mongodb://localhost:27017",
                    "PAYMENTS_DATABASE_NAME": "payments",
                    "PAYMENTS_INTERNAL_SERVICE_TOKEN": "secret",
                    "PAYMENTS_TOSS_WEBHOOK_SECRET": "webhook-secret",
                    "PAYMENTS_NOTIFICATION_RECIPIENT_API_BASE_URL": (
                        "https://member.example.com"
                    ),
                }
            )

    def test_build_http_dependencies_wires_runtime_adapters(self) -> None:
        dependencies = build_http_dependencies(
            motor_database_stub(),
            PaymentHttpConfig(
                database_url="mongodb://localhost:27017",
                database_name="payments",
                internal_service_token="secret",
                toss_webhook_secret="webhook-secret",
                admin_auth_link_base_url="https://admin.example.com",
                notification_recipient_api_base_url="https://member.example.com",
            ),
        )

        assert isinstance(dependencies.catalog_repository, MongoCatalogRepository)
        assert isinstance(dependencies.admin_catalog, MongoAdminCatalogRepository)
        assert isinstance(dependencies.admin_auth, MongoAdminAuthRepository)
        assert isinstance(
            dependencies.admin_auth_uow_factory,
            MongoAdminAuthUnitOfWorkFactory,
        )
        assert isinstance(
            dependencies.admin_auth_rate_limiter,
            InMemoryAdminAuthRateLimiter,
        )
        assert isinstance(
            dependencies.admin_auth_email_sender,
            AdminAuthOutboxEmailSender,
        )
        assert isinstance(
            dependencies.admin_auth_email_sender.outbox_repository,
            MongoNotificationOutboxRepository,
        )
        assert isinstance(
            dependencies.admin_auth_email_sender.template_repository,
            MongoNotificationTemplateRepository,
        )
        assert isinstance(
            dependencies.admin_operations,
            MongoAdminOperationsRepository,
        )
        assert isinstance(
            dependencies.admin_subscription_adjust_uow_factory,
            MongoAdminSubscriptionAdjustUnitOfWorkFactory,
        )
        assert isinstance(dependencies.billing_auths, MongoBillingAuthRepository)
        assert dependencies.toss_client_key == "test_ck_local"
        assert isinstance(dependencies.billing_methods, MongoBillingMethodRepository)
        assert isinstance(
            dependencies.billing_method_default_uow_factory,
            MongoBillingMethodDefaultUnitOfWorkFactory,
        )
        assert isinstance(
            dependencies.billing_method_delete_uow_factory,
            MongoBillingMethodDeleteUnitOfWorkFactory,
        )
        assert isinstance(dependencies.billing_retries, MongoBillingRetryRepository)
        assert isinstance(dependencies.invoices, MongoInvoiceRepository)
        assert isinstance(
            dependencies.idempotency_keys,
            MongoIdempotencyKeyRepository,
        )
        assert isinstance(dependencies.operation_locks, MongoOperationLockRepository)
        assert isinstance(
            dependencies.one_time_payment_uow_factory,
            MongoOneTimePaymentUnitOfWorkFactory,
        )
        assert isinstance(dependencies.payment_attempts, MongoPaymentAttemptRepository)
        assert isinstance(
            dependencies.payment_customers,
            MongoPaymentCustomerRepository,
        )
        assert isinstance(dependencies.payment_provider, TossPaymentProvider)
        assert isinstance(
            dependencies.subscription_accounts,
            MongoSubscriptionAccountRepository,
        )
        assert isinstance(
            dependencies.subscription_billing_uow_factory,
            MongoSubscriptionBillingUnitOfWorkFactory,
        )
        assert isinstance(
            dependencies.subscription_change_tokens,
            HmacSubscriptionChangeTokenCodec,
        )
        assert isinstance(
            dependencies.subscription_change_uow_factory,
            MongoSubscriptionChangeUnitOfWorkFactory,
        )
        assert isinstance(
            dependencies.subscription_checkouts,
            MongoSubscriptionCheckoutRepository,
        )
        assert isinstance(
            dependencies.subscription_cancel_uow_factory,
            MongoSubscriptionCancelUnitOfWorkFactory,
        )
        assert isinstance(
            dependencies.subscription_confirm_uow_factory,
            MongoSubscriptionConfirmUnitOfWorkFactory,
        )
        assert isinstance(
            dependencies.subscription_expirations,
            MongoSubscriptionExpirationRepository,
        )
        assert isinstance(
            dependencies.subscription_expiration_uow_factory,
            MongoSubscriptionExpirationUnitOfWorkFactory,
        )
        assert isinstance(
            dependencies.subscription_resume_uow_factory,
            MongoSubscriptionResumeUnitOfWorkFactory,
        )
        assert isinstance(dependencies.webhooks, MongoWebhookRepository)
        assert isinstance(
            dependencies.webhook_uow_factory,
            MongoWebhookUnitOfWorkFactory,
        )
        assert isinstance(dependencies.billing_key_cipher, FernetBillingKeyCipher)
        assert isinstance(dependencies.clock, SystemClock)
        assert dependencies.internal_service_token == "secret"
        assert dependencies.toss_webhook_secret == "webhook-secret"
        assert dependencies.allowed_redirect_hosts == ("example.com",)

    def test_create_app_includes_health(self) -> None:
        dependencies = build_http_dependencies(
            motor_database_stub(),
            PaymentHttpConfig(
                database_url="mongodb://localhost:27017",
                database_name="payments",
                internal_service_token="secret",
                toss_webhook_secret="webhook-secret",
                admin_auth_link_base_url="https://admin.example.com",
                notification_recipient_api_base_url="https://member.example.com",
            ),
        )

        response = TestClient(create_app(dependencies)).get("/health")

        assert response.status_code == 200
        assert response.json() == {"ok": True}
