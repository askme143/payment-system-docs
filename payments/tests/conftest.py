from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan
from payments.http.composition import create_app
from payments.http.dependencies import HttpDependencies


class FixedClock:
    def utc_now(self) -> datetime:
        return datetime(2026, 6, 10, 0, 0, tzinfo=UTC)


class FakeCatalogRepository:
    def __init__(self) -> None:
        self.product = Product(
            id="product_basic",
            product_code="basic",
            product_type="subscription",
            name="Basic",
            status="active",
        )
        self.plan = SubscriptionPlan(
            id="plan_basic_monthly",
            product_id="product_basic",
            plan_code="basic_monthly",
            billing_period="monthly",
            amount=9900,
            entitlements={"seats": 1},
            status="active",
        )

    async def list_active_subscription_catalog(self):
        return [(self.product, self.plan)]

    async def get_active_subscription_plan(self, plan_id: str):
        if plan_id == self.plan.id:
            return (self.product, self.plan)
        return None


class FakePaymentRepository:
    def __init__(self) -> None:
        self.idempotency_keys = {}
        self.checkouts = {}
        self.payments = {}

    async def find_idempotency_key(self, scope: str, key_hash: str):
        return self.idempotency_keys.get((scope, key_hash))

    async def save_idempotency_key(self, key) -> None:
        self.idempotency_keys[(key.scope, key.key_hash)] = key

    async def save_checkout(self, checkout) -> None:
        self.checkouts[checkout.id] = checkout

    async def save_payment(self, payment) -> None:
        self.payments[payment.id] = payment

    async def get_checkout_for_user(self, checkout_id: str, user_id: str):
        checkout = self.checkouts.get(checkout_id)
        if checkout and checkout.user_id == user_id:
            return checkout
        return None

    async def get_payment_for_user(self, payment_id: str, user_id: str):
        payment = self.payments.get(payment_id)
        if payment is None or payment.checkout_id is None:
            return None
        checkout = self.checkouts.get(payment.checkout_id)
        if checkout and checkout.user_id == user_id:
            return payment
        return None


@dataclass(frozen=True, slots=True)
class TestDependencies:
    catalog_repository: FakeCatalogRepository
    payment_repository: FakePaymentRepository
    clock: FixedClock


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer secret",
        "X-Request-Id": "req_test",
        "X-Request-User-Id": "user_1",
    }


@pytest.fixture
def test_dependencies() -> TestDependencies:
    return TestDependencies(
        catalog_repository=FakeCatalogRepository(),
        payment_repository=FakePaymentRepository(),
        clock=FixedClock(),
    )


@pytest.fixture
def client(test_dependencies: TestDependencies) -> Iterator[TestClient]:
    dependencies = HttpDependencies(
        catalog_repository=test_dependencies.catalog_repository,
        payment_repository=test_dependencies.payment_repository,
        clock=test_dependencies.clock,
        internal_service_token="secret",
    )
    with TestClient(create_app(dependencies)) as test_client:
        yield test_client
