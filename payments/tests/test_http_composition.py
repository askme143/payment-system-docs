from __future__ import annotations

from typing import cast

import pytest
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorDatabase

from payments.adapters.mongo import (
    MongoCatalogRepository,
    MongoOneTimePaymentUnitOfWorkFactory,
    MongoPaymentAttemptRepository,
)
from payments.adapters.time import SystemClock
from payments.http.composition import build_http_dependencies, create_app
from payments.http.config import PaymentHttpConfig, payment_config_from_env

TestMongoDocument = dict[str, object]


class FakeDatabase:
    products = object()
    subscription_plans = object()
    one_time_skus = object()
    checkouts = object()
    payments = object()
    idempotency_keys = object()


def motor_database_stub() -> AsyncIOMotorDatabase[TestMongoDocument]:
    return cast(AsyncIOMotorDatabase[TestMongoDocument], FakeDatabase())


class TestHttpComposition:
    def test_payment_config_from_env_loads_required_values(self) -> None:
        config = payment_config_from_env(
            {
                "PAYMENTS_DATABASE_URL": "mongodb://localhost:27017",
                "PAYMENTS_DATABASE_NAME": "payments",
                "PAYMENTS_INTERNAL_SERVICE_TOKEN": "secret",
            }
        )

        assert config.database_url == "mongodb://localhost:27017"
        assert config.database_name == "payments"
        assert config.internal_service_token == "secret"

    def test_payment_config_from_env_rejects_missing_required_values(self) -> None:
        with pytest.raises(ValueError, match="PAYMENTS_DATABASE_URL"):
            payment_config_from_env({})

    def test_build_http_dependencies_wires_runtime_adapters(self) -> None:
        dependencies = build_http_dependencies(
            motor_database_stub(),
            PaymentHttpConfig(
                database_url="mongodb://localhost:27017",
                database_name="payments",
                internal_service_token="secret",
            ),
        )

        assert isinstance(dependencies.catalog_repository, MongoCatalogRepository)
        assert isinstance(
            dependencies.one_time_payment_uow_factory,
            MongoOneTimePaymentUnitOfWorkFactory,
        )
        assert isinstance(dependencies.payment_attempts, MongoPaymentAttemptRepository)
        assert isinstance(dependencies.clock, SystemClock)
        assert dependencies.internal_service_token == "secret"

    def test_create_app_includes_health(self) -> None:
        dependencies = build_http_dependencies(
            motor_database_stub(),
            PaymentHttpConfig(
                database_url="mongodb://localhost:27017",
                database_name="payments",
                internal_service_token="secret",
            ),
        )

        response = TestClient(create_app(dependencies)).get("/health")

        assert response.status_code == 200
        assert response.json() == {"ok": True}
