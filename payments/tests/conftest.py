from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest
from fastapi.testclient import TestClient

from payments.application.ports import (
    CheckoutRepository,
    IdempotencyKeyRepository,
    OneTimePaymentUnitOfWork,
    OneTimePaymentUnitOfWorkFactory,
    OneTimeSkuRepository,
    PaymentAttemptRepository,
)
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.payment import Payment
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


class FakeIdempotencyKeyRepository(IdempotencyKeyRepository):
    def __init__(self) -> None:
        self.idempotency_keys: dict[tuple[str, str], IdempotencyKey] = {}

    async def find_idempotency_key(
        self,
        scope: str,
        key_hash: str,
    ) -> IdempotencyKey | None:
        return self.idempotency_keys.get((scope, key_hash))

    async def save_idempotency_key(self, key: IdempotencyKey) -> None:
        self.idempotency_keys[(key.scope, key.key_hash)] = key


class FakeCheckoutRepository(CheckoutRepository):
    def __init__(self) -> None:
        self.checkouts: dict[str, Checkout] = {}

    async def save_checkout(self, checkout: Checkout) -> None:
        self.checkouts[checkout.id] = checkout

    async def get_checkout_for_user(
        self,
        checkout_id: str,
        user_id: str,
    ) -> Checkout | None:
        checkout = self.checkouts.get(checkout_id)
        if checkout and checkout.user_id == user_id:
            return checkout
        return None


class FakePaymentAttemptRepository(PaymentAttemptRepository):
    def __init__(self, checkouts: FakeCheckoutRepository) -> None:
        self._checkouts = checkouts
        self.payments: dict[str, Payment] = {}

    async def save_payment(self, payment: Payment) -> None:
        self.payments[payment.id] = payment

    async def get_payment_for_user(
        self,
        payment_id: str,
        user_id: str,
    ) -> Payment | None:
        payment = self.payments.get(payment_id)
        if payment is None or payment.checkout_id is None:
            return None
        checkout = self._checkouts.checkouts.get(payment.checkout_id)
        if checkout and checkout.user_id == user_id:
            return payment
        return None


class FakeOneTimeSkuRepository(OneTimeSkuRepository):
    def __init__(self) -> None:
        self.one_time_skus = {
            "sku_report_pack_100": OneTimeSku(
                id="sku_report_pack_100",
                product_id="product_reports",
                sku_code="REPORT_PACK_100",
                amount=25000,
                stock_policy="unlimited",
                status="active",
            )
        }

    async def get_active_one_time_sku(self, sku_id: str) -> OneTimeSku | None:
        sku = self.one_time_skus.get(sku_id)
        if sku and sku.status == "active":
            return sku
        return None

    async def reserve_one_time_sku_stock(
        self,
        sku: OneTimeSku,
        quantity: int,
    ) -> bool:
        if sku.stock_policy == "unlimited":
            return True
        available_stock = sku.available_stock
        if available_stock is None or available_stock < quantity:
            return False
        sku.reserved_stock = (sku.reserved_stock or 0) + quantity
        return True


@dataclass(frozen=True, slots=True)
class FakePaymentStores:
    idempotency_keys: FakeIdempotencyKeyRepository
    checkouts: FakeCheckoutRepository
    payments: FakePaymentAttemptRepository
    one_time_skus: FakeOneTimeSkuRepository


class FakeOneTimePaymentUnitOfWork(OneTimePaymentUnitOfWork):
    def __init__(self, stores: FakePaymentStores) -> None:
        self.idempotency_keys: IdempotencyKeyRepository = stores.idempotency_keys
        self.checkouts: CheckoutRepository = stores.checkouts
        self.payments: PaymentAttemptRepository = stores.payments
        self.one_time_skus: OneTimeSkuRepository = stores.one_time_skus

    async def __aenter__(self) -> FakeOneTimePaymentUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeOneTimePaymentUnitOfWorkFactory(OneTimePaymentUnitOfWorkFactory):
    def __init__(self, stores: FakePaymentStores) -> None:
        self._stores = stores

    def __call__(self) -> FakeOneTimePaymentUnitOfWork:
        return FakeOneTimePaymentUnitOfWork(self._stores)


@dataclass(frozen=True, slots=True)
class TestDependencies:
    catalog_repository: FakeCatalogRepository
    payment_stores: FakePaymentStores
    one_time_payment_uow_factory: FakeOneTimePaymentUnitOfWorkFactory
    payment_attempts: FakePaymentAttemptRepository
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
    checkouts = FakeCheckoutRepository()
    payment_attempts = FakePaymentAttemptRepository(checkouts)
    payment_stores = FakePaymentStores(
        idempotency_keys=FakeIdempotencyKeyRepository(),
        checkouts=checkouts,
        payments=payment_attempts,
        one_time_skus=FakeOneTimeSkuRepository(),
    )
    return TestDependencies(
        catalog_repository=FakeCatalogRepository(),
        payment_stores=payment_stores,
        one_time_payment_uow_factory=FakeOneTimePaymentUnitOfWorkFactory(
            payment_stores
        ),
        payment_attempts=payment_attempts,
        clock=FixedClock(),
    )


@pytest.fixture
def client(test_dependencies: TestDependencies) -> Iterator[TestClient]:
    dependencies = HttpDependencies(
        catalog_repository=test_dependencies.catalog_repository,
        one_time_payment_uow_factory=(
            test_dependencies.one_time_payment_uow_factory
        ),
        payment_attempts=test_dependencies.payment_attempts,
        clock=test_dependencies.clock,
        internal_service_token="secret",
    )
    with TestClient(create_app(dependencies)) as test_client:
        yield test_client
