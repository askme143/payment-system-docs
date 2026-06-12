from __future__ import annotations

from datetime import UTC, datetime

import pytest

from payments.application.admin_catalog import (
    AdminOneTimeSkuCreateCommand,
    AdminOneTimeSkuUpdateCommand,
    AdminProductCreateCommand,
    AdminProductStatusChangeCommand,
    AdminRequestContext,
    AdminSubscriptionPlanCreateCommand,
    AdminSubscriptionPlanUpdateCommand,
    change_admin_product_status,
    create_admin_one_time_sku,
    create_admin_product,
    create_admin_subscription_plan,
    update_admin_one_time_sku,
    update_admin_subscription_plan,
)
from payments.application.errors import (
    BadRequestError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan


class FixedClock:
    def utc_now(self) -> datetime:
        return datetime(2026, 6, 10, 0, 0, tzinfo=UTC)


class FakeAdminCatalogRepository:
    def __init__(self) -> None:
        self.products: dict[str, Product] = {}
        self.subscription_plans: dict[str, SubscriptionPlan] = {}
        self.one_time_skus: dict[str, OneTimeSku] = {}
        self.audit_records: list[dict[str, object]] = []
        self.active_subscription_plan_counts: dict[str, int] = {}
        self.active_one_time_sku_counts: dict[str, int] = {}

    async def get_product_by_code(
        self,
        product_code: str,
        product_type: str,
    ) -> Product | None:
        return next(
            (
                product
                for product in self.products.values()
                if product.product_code == product_code
                and product.product_type == product_type
            ),
            None,
        )

    async def save_product(self, product: Product) -> None:
        self.products[product.id] = product

    async def get_product(self, product_id: str) -> Product | None:
        return self.products.get(product_id)

    async def count_active_subscription_plans(self, product_id: str) -> int:
        return self.active_subscription_plan_counts.get(
            product_id,
            sum(
                1
                for plan in self.subscription_plans.values()
                if plan.product_id == product_id and plan.status == "active"
            ),
        )

    async def count_active_one_time_skus(self, product_id: str) -> int:
        return self.active_one_time_sku_counts.get(
            product_id,
            sum(
                1
                for sku in self.one_time_skus.values()
                if sku.product_id == product_id and sku.status == "active"
            ),
        )

    async def get_subscription_plan(
        self,
        product_id: str,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        plan = self.subscription_plans.get(plan_id)
        if plan is None or plan.product_id != product_id:
            return None
        return plan

    async def get_subscription_plan_by_code(
        self,
        product_id: str,
        plan_code: str,
    ) -> SubscriptionPlan | None:
        return next(
            (
                plan
                for plan in self.subscription_plans.values()
                if plan.product_id == product_id and plan.plan_code == plan_code
            ),
            None,
        )

    async def save_subscription_plan(self, plan: SubscriptionPlan) -> None:
        self.subscription_plans[plan.id] = plan

    async def get_one_time_sku(
        self,
        product_id: str,
        sku_id: str,
    ) -> OneTimeSku | None:
        sku = self.one_time_skus.get(sku_id)
        if sku is None or sku.product_id != product_id:
            return None
        return sku

    async def get_one_time_sku_by_code(
        self,
        product_id: str,
        sku_code: str,
    ) -> OneTimeSku | None:
        return next(
            (
                sku
                for sku in self.one_time_skus.values()
                if sku.product_id == product_id and sku.sku_code == sku_code
            ),
            None,
        )

    async def save_one_time_sku(self, sku: OneTimeSku) -> None:
        self.one_time_skus[sku.id] = sku

    async def save_product_audit_record(
        self,
        *,
        product_id: str,
        admin_id: str,
        request_id: str,
        action: str,
        previous: dict[str, object] | None,
        next_value: dict[str, object],
        request_ip: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.audit_records.append(
            {
                "product_id": product_id,
                "admin_id": admin_id,
                "request_id": request_id,
                "action": action,
                "previous": previous,
                "next": next_value,
                "request_ip": request_ip,
                "created_at": created_at,
            }
        )


async def test_create_admin_product_persists_draft_product_and_audit() -> None:
    repository = FakeAdminCatalogRepository()

    product = await create_admin_product(
        AdminProductCreateCommand(
            product_code="ANALYTICS",
            product_type="subscription",
            name="Analytics",
            status="draft",
        ),
        AdminRequestContext(
            request_id="req_admin",
            admin_id="admin_1",
            request_ip="203.0.113.30",
        ),
        repository,
    )

    assert product.product_code == "ANALYTICS"
    assert product.product_type == "subscription"
    assert product.status == "draft"
    assert repository.products[product.id] == product
    assert repository.audit_records == [
        {
            "product_id": product.id,
            "admin_id": "admin_1",
            "request_id": "req_admin",
            "action": "product.create",
            "previous": None,
            "next": {
                "product_id": product.id,
                "product_code": "ANALYTICS",
                "product_type": "subscription",
                "name": "Analytics",
                "status": "draft",
            },
            "request_ip": "203.0.113.30",
            "created_at": None,
        }
    ]


async def test_create_admin_product_rejects_invalid_product_code_format() -> None:
    repository = FakeAdminCatalogRepository()

    for product_code in ("1ANALYTICS", "ANALYTICS-PRO", "ANALYTICS PRO"):
        with pytest.raises(BadRequestError):
            await create_admin_product(
                AdminProductCreateCommand(
                    product_code=product_code,
                    product_type="subscription",
                    name="Analytics",
                    status="draft",
                ),
                AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
                repository,
            )

    assert repository.products == {}
    assert repository.audit_records == []


async def test_create_admin_product_rejects_duplicate_product_code() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_existing"] = Product(
        id="product_existing",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    with pytest.raises(InvalidStateTransitionError):
        await create_admin_product(
            AdminProductCreateCommand(
                product_code="ANALYTICS",
                product_type="subscription",
                name="Analytics",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )


async def test_create_admin_product_allows_same_code_for_different_type() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_subscription"] = Product(
        id="product_subscription",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics subscription",
        status="draft",
    )

    product = await create_admin_product(
        AdminProductCreateCommand(
            product_code="ANALYTICS",
            product_type="one_time",
            name="Analytics reports",
        ),
        AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        repository,
    )

    assert product.product_code == "ANALYTICS"
    assert product.product_type == "one_time"
    assert repository.products[product.id] == product


async def test_create_admin_product_rejects_non_draft_initial_status() -> None:
    repository = FakeAdminCatalogRepository()

    with pytest.raises(BadRequestError, match="draft"):
        await create_admin_product(
            AdminProductCreateCommand(
                product_code="ANALYTICS",
                product_type="subscription",
                name="Analytics",
                status="active",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.products == {}
    assert repository.audit_records == []


async def test_change_admin_product_status_activates_product_with_selling_unit(
) -> None:
    fixed_clock = FixedClock()
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    repository.active_subscription_plan_counts["product_analytics"] = 1

    result = await change_admin_product_status(
        "product_analytics",
        AdminProductStatusChangeCommand(
            status="active",
            reason="launch approved",
        ),
        AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        repository,
        fixed_clock,
    )

    assert result.product.status == "active"
    assert result.previous_status == "draft"
    assert result.effective_at == fixed_clock.utc_now()
    assert repository.products["product_analytics"].status == "active"
    assert repository.audit_records[-1]["action"] == "product.status_change"
    assert repository.audit_records[-1]["created_at"] == fixed_clock.utc_now()
    assert repository.audit_records[-1]["previous"] == {"status": "draft"}
    assert repository.audit_records[-1]["next"] == {
        "status": "active",
        "reason": "launch approved",
        "effective_at": fixed_clock.utc_now(),
    }


async def test_change_admin_product_status_uses_documented_effective_at() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="active",
    )

    result = await change_admin_product_status(
        "product_analytics",
        AdminProductStatusChangeCommand(
            status="paused",
            reason="incident response",
            effective_at="2026-06-08T10:00:00+09:00",
        ),
        AdminRequestContext(
            request_id="req_admin",
            admin_id="admin_1",
            request_ip="203.0.113.30",
        ),
        repository,
        FixedClock(),
    )

    assert result.product.status == "paused"
    assert result.effective_at == datetime.fromisoformat("2026-06-08T10:00:00+09:00")
    audit_record = repository.audit_records[-1]
    assert audit_record["request_ip"] == "203.0.113.30"
    assert audit_record["created_at"] == FixedClock().utc_now()
    assert audit_record["next"] == {
        "status": "paused",
        "reason": "incident response",
        "effective_at": datetime.fromisoformat("2026-06-08T10:00:00+09:00"),
    }


@pytest.mark.parametrize(
    "command",
    [
        AdminProductStatusChangeCommand(
            status=None,
            reason="launch approved",
        ),
        AdminProductStatusChangeCommand(
            status="deleted",
            reason="launch approved",
        ),
        AdminProductStatusChangeCommand(
            status="paused",
            reason=None,
        ),
        AdminProductStatusChangeCommand(
            status="paused",
            reason=" ",
        ),
        AdminProductStatusChangeCommand(
            status="paused",
            reason="incident response",
            effective_at="not-a-date",
        ),
        AdminProductStatusChangeCommand(
            status="paused",
            reason="incident response",
            effective_at="2026-06-08T10:00:00",
        ),
        AdminProductStatusChangeCommand(
            status="paused",
            reason="incident response",
            effective_at=123,
        ),
    ],
)
async def test_change_admin_product_status_rejects_invalid_contract_values(
    command: AdminProductStatusChangeCommand,
) -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="active",
    )

    with pytest.raises(BadRequestError):
        await change_admin_product_status(
            "product_analytics",
            command,
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
            FixedClock(),
        )

    assert repository.products["product_analytics"].status == "active"
    assert repository.audit_records == []


async def test_change_admin_product_status_rejects_archived_reactivation() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="archived",
    )
    repository.active_subscription_plan_counts["product_analytics"] = 1

    with pytest.raises(InvalidStateTransitionError, match="archived"):
        await change_admin_product_status(
            "product_analytics",
            AdminProductStatusChangeCommand(
                status="active",
                reason="reactivate",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
            FixedClock(),
        )

    assert repository.products["product_analytics"].status == "archived"
    assert repository.audit_records == []


async def test_change_admin_product_status_rejects_return_to_draft() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="active",
    )

    with pytest.raises(InvalidStateTransitionError, match="draft"):
        await change_admin_product_status(
            "product_analytics",
            AdminProductStatusChangeCommand(
                status="draft",
                reason="undo launch",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
            FixedClock(),
        )

    assert repository.products["product_analytics"].status == "active"
    assert repository.audit_records == []


async def test_change_admin_product_status_rejects_active_without_selling_unit(
) -> None:
    fixed_clock = FixedClock()
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    with pytest.raises(InvalidStateTransitionError):
        await change_admin_product_status(
            "product_analytics",
            AdminProductStatusChangeCommand(
                status="active",
                reason="launch approved",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
            fixed_clock,
        )


async def test_change_admin_product_status_raises_for_missing_product(
) -> None:
    fixed_clock = FixedClock()
    with pytest.raises(ResourceNotFoundError):
        await change_admin_product_status(
            "missing",
            AdminProductStatusChangeCommand(
                status="paused",
                reason="maintenance",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            FakeAdminCatalogRepository(),
            fixed_clock,
        )


async def test_create_admin_subscription_plan_persists_plan_and_audit() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    plan = await create_admin_subscription_plan(
        "product_analytics",
        AdminSubscriptionPlanCreateCommand(
            plan_code="ANALYTICS_BASIC_MONTHLY",
            billing_period="monthly",
            amount=9900,
            currency="KRW",
            status="active",
            entitlements={"seatLimit": 3},
        ),
        AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        repository,
    )

    assert plan.product_id == "product_analytics"
    assert plan.plan_code == "ANALYTICS_BASIC_MONTHLY"
    assert plan.amount == 9900
    assert plan.currency == "KRW"
    assert repository.subscription_plans[plan.id] == plan
    assert repository.audit_records[-1]["action"] == "subscription_plan.create"


async def test_create_admin_subscription_plan_rejects_invalid_amount() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    with pytest.raises(BadRequestError, match="amount"):
        await create_admin_subscription_plan(
            "product_analytics",
            AdminSubscriptionPlanCreateCommand(
                plan_code="ANALYTICS_BASIC_MONTHLY",
                billing_period="monthly",
                amount=0,
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.subscription_plans == {}


async def test_create_admin_subscription_plan_rejects_invalid_documented_values() -> (
    None
):
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    invalid_commands = [
        AdminSubscriptionPlanCreateCommand(
            plan_code="ANALYTICS-BASIC-MONTHLY",
            billing_period="monthly",
            amount=9900,
        ),
        AdminSubscriptionPlanCreateCommand(
            plan_code="ANALYTICS_BASIC_MONTHLY",
            billing_period="weekly",
            amount=9900,
        ),
        AdminSubscriptionPlanCreateCommand(
            plan_code="ANALYTICS_BASIC_MONTHLY",
            billing_period="monthly",
            amount=9900,
            currency="USD",
        ),
        AdminSubscriptionPlanCreateCommand(
            plan_code="ANALYTICS_BASIC_MONTHLY",
            billing_period="monthly",
            amount=9900,
            status="deleted",
        ),
        AdminSubscriptionPlanCreateCommand(
            plan_code="ANALYTICS_BASIC_MONTHLY",
            billing_period="monthly",
            amount=9900,
            entitlements=["seatLimit"],
        ),
    ]

    for command in invalid_commands:
        with pytest.raises(BadRequestError):
            await create_admin_subscription_plan(
                "product_analytics",
                command,
                AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
                repository,
            )

    assert repository.subscription_plans == {}


async def test_create_admin_subscription_plan_rejects_duplicate_plan_code() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    repository.subscription_plans["plan_basic"] = SubscriptionPlan(
        id="plan_basic",
        product_id="product_analytics",
        plan_code="ANALYTICS_BASIC_MONTHLY",
        billing_period="monthly",
        amount=9900,
        entitlements={},
        status="active",
    )

    with pytest.raises(InvalidStateTransitionError, match="already exists"):
        await create_admin_subscription_plan(
            "product_analytics",
            AdminSubscriptionPlanCreateCommand(
                plan_code="ANALYTICS_BASIC_MONTHLY",
                billing_period="monthly",
                amount=12900,
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert list(repository.subscription_plans) == ["plan_basic"]


async def test_create_admin_subscription_plan_rejects_one_time_product() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    with pytest.raises(InvalidStateTransitionError):
        await create_admin_subscription_plan(
            "product_reports",
            AdminSubscriptionPlanCreateCommand(
                plan_code="REPORTS_BASIC_MONTHLY",
                billing_period="monthly",
                amount=9900,
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )


async def test_update_admin_subscription_plan_changes_new_cycle_policy() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    repository.subscription_plans["plan_basic"] = SubscriptionPlan(
        id="plan_basic",
        product_id="product_analytics",
        plan_code="ANALYTICS_BASIC_MONTHLY",
        billing_period="monthly",
        amount=9900,
        entitlements={"seatLimit": 3},
        status="active",
        currency="KRW",
        version=1,
    )

    result = await update_admin_subscription_plan(
        "product_analytics",
        "plan_basic",
        AdminSubscriptionPlanUpdateCommand(
            amount=12900,
            currency="KRW",
            status="active",
            entitlements={"seatLimit": 5},
            change_reason="seat and price adjustment",
        ),
        AdminRequestContext(
            request_id="req_admin",
            admin_id="admin_1",
            request_ip="203.0.113.40",
        ),
        repository,
    )

    assert result.plan.amount == 12900
    assert result.plan.entitlements == {"seatLimit": 5}
    assert result.plan.version == 2
    assert result.effective_for == "new_subscriptions_and_next_cycles"
    assert repository.audit_records[-1]["action"] == "subscription_plan.update"
    assert repository.audit_records[-1]["previous"] == {
        "plan_id": "plan_basic",
        "product_id": "product_analytics",
        "plan_code": "ANALYTICS_BASIC_MONTHLY",
        "billing_period": "monthly",
        "amount": 9900,
        "currency": "KRW",
        "status": "active",
        "entitlements": {"seatLimit": 3},
        "version": 1,
    }
    next_value = repository.audit_records[-1]["next"]
    assert isinstance(next_value, dict)
    assert next_value["change_reason"] == "seat and price adjustment"
    assert repository.audit_records[-1]["request_ip"] == "203.0.113.40"


async def test_update_admin_subscription_plan_rejects_empty_update() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    repository.subscription_plans["plan_basic"] = SubscriptionPlan(
        id="plan_basic",
        product_id="product_analytics",
        plan_code="ANALYTICS_BASIC_MONTHLY",
        billing_period="monthly",
        amount=9900,
        entitlements={"seatLimit": 3},
        status="active",
        currency="KRW",
        version=1,
    )

    with pytest.raises(BadRequestError, match="no subscription plan fields"):
        await update_admin_subscription_plan(
            "product_analytics",
            "plan_basic",
            AdminSubscriptionPlanUpdateCommand(
                change_reason="audit only is not a policy change",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.subscription_plans["plan_basic"].version == 1
    assert repository.audit_records == []


async def test_update_admin_subscription_plan_rejects_invalid_documented_values() -> (
    None
):
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    repository.subscription_plans["plan_basic"] = SubscriptionPlan(
        id="plan_basic",
        product_id="product_analytics",
        plan_code="ANALYTICS_BASIC_MONTHLY",
        billing_period="monthly",
        amount=9900,
        entitlements={"seatLimit": 3},
        status="active",
        currency="KRW",
        version=1,
    )

    invalid_commands = [
        AdminSubscriptionPlanUpdateCommand(
            amount=0,
            change_reason="invalid amount",
        ),
        AdminSubscriptionPlanUpdateCommand(
            amount="12900",
            change_reason="invalid amount type",
        ),
        AdminSubscriptionPlanUpdateCommand(
            currency="USD",
            change_reason="invalid currency",
        ),
        AdminSubscriptionPlanUpdateCommand(
            status="deleted",
            change_reason="invalid status",
        ),
        AdminSubscriptionPlanUpdateCommand(
            entitlements=["seatLimit"],
            change_reason="invalid entitlements",
        ),
    ]

    for command in invalid_commands:
        with pytest.raises(BadRequestError):
            await update_admin_subscription_plan(
                "product_analytics",
                "plan_basic",
                command,
                AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
                repository,
            )

    assert repository.subscription_plans["plan_basic"].version == 1
    assert repository.audit_records == []


async def test_update_admin_subscription_plan_rejects_missing_plan() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    with pytest.raises(ResourceNotFoundError, match="subscription plan not found"):
        await update_admin_subscription_plan(
            "product_analytics",
            "missing_plan",
            AdminSubscriptionPlanUpdateCommand(
                amount=12900,
                change_reason="price adjustment",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )


async def test_update_admin_subscription_plan_rejects_one_time_product() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    with pytest.raises(InvalidStateTransitionError):
        await update_admin_subscription_plan(
            "product_reports",
            "plan_basic",
            AdminSubscriptionPlanUpdateCommand(
                amount=12900,
                change_reason="price adjustment",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )


async def test_create_admin_one_time_sku_persists_unlimited_sku_and_audit() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    sku = await create_admin_one_time_sku(
        "product_reports",
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
                amount=50000,
                currency="KRW",
                status="active",
                stock_policy={"type": "unlimited"},
                purchase_limit={"perUser": 5},
        ),
        AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        repository,
    )

    assert sku.product_id == "product_reports"
    assert sku.sku_code == "REPORT_PACK_100"
    assert sku.stock_policy == "unlimited"
    assert sku.currency == "KRW"
    assert repository.one_time_skus[sku.id] == sku
    assert repository.audit_records[-1]["action"] == "one_time_sku.create"


async def test_create_admin_one_time_sku_persists_limited_stock_counters() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    sku = await create_admin_one_time_sku(
        "product_reports",
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            status="active",
            stock_policy={"type": "limited"},
            total_stock=800,
        ),
        AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        repository,
    )

    assert sku.stock_policy == "limited"
    assert sku.total_stock == 800
    assert sku.reserved_stock == 0
    assert sku.sold_stock == 0
    assert sku.available_stock == 800
    next_value = repository.audit_records[-1]["next"]
    assert isinstance(next_value, dict)
    assert next_value["total_stock"] == 800


async def test_create_admin_one_time_sku_rejects_invalid_limited_stock() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    with pytest.raises(BadRequestError, match="totalStock"):
        await create_admin_one_time_sku(
            "product_reports",
            AdminOneTimeSkuCreateCommand(
                sku_code="REPORT_PACK_100",
                amount=50000,
                stock_policy={"type": "limited"},
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.one_time_skus == {}


async def test_create_admin_one_time_sku_rejects_invalid_documented_values() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    invalid_commands = [
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT-PACK-100",
            amount=50000,
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount="50000",
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=0,
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            currency="USD",
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            status="deleted",
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            stock_policy={"type": "external"},
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            stock_policy="limited",
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            stock_policy={"type": "limited"},
            total_stock="800",
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            stock_policy={"type": "unlimited"},
            total_stock=800,
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            purchase_limit={"perUser": 0},
        ),
        AdminOneTimeSkuCreateCommand(
            sku_code="REPORT_PACK_100",
            amount=50000,
            purchase_limit=["perUser"],
        ),
    ]

    for command in invalid_commands:
        with pytest.raises(BadRequestError):
            await create_admin_one_time_sku(
                "product_reports",
                command,
                AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
                repository,
            )

    assert repository.one_time_skus == {}


async def test_create_admin_one_time_sku_rejects_duplicate_sku_code() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    repository.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="unlimited",
        status="active",
    )

    with pytest.raises(InvalidStateTransitionError, match="already exists"):
        await create_admin_one_time_sku(
            "product_reports",
            AdminOneTimeSkuCreateCommand(
                sku_code="REPORT_PACK_100",
                amount=60000,
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert list(repository.one_time_skus) == ["sku_pack"]


async def test_create_admin_one_time_sku_rejects_missing_product() -> None:
    repository = FakeAdminCatalogRepository()

    with pytest.raises(ResourceNotFoundError, match="product not found"):
        await create_admin_one_time_sku(
            "missing_product",
            AdminOneTimeSkuCreateCommand(
                sku_code="REPORT_PACK_100",
                amount=50000,
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )


async def test_create_admin_one_time_sku_rejects_subscription_product() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    with pytest.raises(InvalidStateTransitionError):
        await create_admin_one_time_sku(
            "product_analytics",
            AdminOneTimeSkuCreateCommand(
                sku_code="REPORT_PACK_100",
                amount=50000,
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )


async def test_update_admin_one_time_sku_updates_stock_policy() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    repository.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="unlimited",
        status="active",
        currency="KRW",
    )

    result = await update_admin_one_time_sku(
        "product_reports",
        "sku_pack",
        AdminOneTimeSkuUpdateCommand(
            amount=55000,
            currency="KRW",
            status="active",
            stock_policy={"type": "limited"},
            total_stock=800,
            purchase_limit={"perUser": 3},
            change_reason="promotion ended",
        ),
        AdminRequestContext(
            request_id="req_admin",
            admin_id="admin_1",
            request_ip="203.0.113.10",
        ),
        repository,
    )

    assert result.sku.amount == 55000
    assert result.sku.stock_policy == "limited"
    assert result.sku.total_stock == 800
    assert result.sku.reserved_stock == 0
    assert result.sku.sold_stock == 0
    assert result.sku.available_stock == 800
    assert result.effective_for == "new_orders"
    audit_record = repository.audit_records[-1]
    previous = audit_record["previous"]
    next_value = audit_record["next"]
    assert isinstance(previous, dict)
    assert isinstance(next_value, dict)
    assert audit_record["action"] == "one_time_sku.update"
    assert audit_record["request_ip"] == "203.0.113.10"
    assert previous["stock_policy"] == "unlimited"
    assert next_value["stock_policy"] == "limited"
    assert next_value["change_reason"] == "promotion ended"


async def test_update_admin_one_time_sku_rejects_empty_update() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    repository.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="unlimited",
        status="active",
        currency="KRW",
    )

    with pytest.raises(BadRequestError, match="no SKU fields"):
        await update_admin_one_time_sku(
            "product_reports",
            "sku_pack",
            AdminOneTimeSkuUpdateCommand(
                change_reason="audit only is not a policy change",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.one_time_skus["sku_pack"].amount == 50000
    assert repository.audit_records == []


@pytest.mark.parametrize(
    ("command", "error_match"),
    [
        (
            AdminOneTimeSkuUpdateCommand(
                amount=0,
                change_reason="invalid value",
            ),
            "amount",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                amount="55000",
                change_reason="invalid value",
            ),
            "amount",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                currency="USD",
                change_reason="invalid value",
            ),
            "currency",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                status="deleted",
                change_reason="invalid value",
            ),
            "status",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                stock_policy={"type": "external"},
                change_reason="invalid value",
            ),
            "stockPolicy",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                stock_policy="limited",
                change_reason="invalid value",
            ),
            "stockPolicy",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                total_stock="800",
                change_reason="invalid value",
            ),
            "totalStock",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                total_stock=800,
                change_reason="invalid value",
            ),
            "totalStock",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                purchase_limit={"perOrder": False},
                change_reason="invalid value",
            ),
            "purchaseLimit",
        ),
        (
            AdminOneTimeSkuUpdateCommand(
                purchase_limit=["perOrder"],
                change_reason="invalid value",
            ),
            "purchaseLimit",
        ),
    ],
)
async def test_update_admin_one_time_sku_rejects_invalid_contract_values(
    command: AdminOneTimeSkuUpdateCommand,
    error_match: str,
) -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    repository.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="unlimited",
        status="active",
        currency="KRW",
    )

    with pytest.raises(BadRequestError, match=error_match):
        await update_admin_one_time_sku(
            "product_reports",
            "sku_pack",
            command,
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.one_time_skus["sku_pack"].purchase_limit is None
    assert repository.one_time_skus["sku_pack"].amount == 50000
    assert repository.one_time_skus["sku_pack"].stock_policy == "unlimited"
    assert repository.audit_records == []


async def test_update_admin_one_time_sku_rejects_missing_sku() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    with pytest.raises(ResourceNotFoundError, match="one-time sku not found"):
        await update_admin_one_time_sku(
            "product_reports",
            "missing_sku",
            AdminOneTimeSkuUpdateCommand(
                amount=55000,
                change_reason="price correction",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.audit_records == []


async def test_update_admin_one_time_sku_rejects_subscription_product() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    with pytest.raises(InvalidStateTransitionError, match="one_time"):
        await update_admin_one_time_sku(
            "product_analytics",
            "sku_pack",
            AdminOneTimeSkuUpdateCommand(
                amount=55000,
                change_reason="wrong API",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    assert repository.audit_records == []


async def test_update_sku_rejects_total_stock_below_committed_stock() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    repository.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="limited",
        status="active",
        currency="KRW",
        total_stock=100,
        reserved_stock=40,
        sold_stock=30,
    )

    with pytest.raises(InvalidStateTransitionError, match="committed stock"):
        await update_admin_one_time_sku(
            "product_reports",
            "sku_pack",
            AdminOneTimeSkuUpdateCommand(
                total_stock=60,
                change_reason="over-tighten stock",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    stored_sku = repository.one_time_skus["sku_pack"]
    assert stored_sku.total_stock == 100
    assert stored_sku.reserved_stock == 40
    assert stored_sku.sold_stock == 30
    assert repository.audit_records == []


async def test_update_sku_rejects_unlimited_with_committed_stock() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    repository.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="limited",
        status="active",
        currency="KRW",
        total_stock=100,
        reserved_stock=10,
        sold_stock=0,
    )

    with pytest.raises(InvalidStateTransitionError, match="stock policy conflicts"):
        await update_admin_one_time_sku(
            "product_reports",
            "sku_pack",
            AdminOneTimeSkuUpdateCommand(
                stock_policy={"type": "unlimited"},
                change_reason="remove stock limit",
            ),
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            repository,
        )

    stored_sku = repository.one_time_skus["sku_pack"]
    assert stored_sku.stock_policy == "limited"
    assert stored_sku.total_stock == 100
    assert stored_sku.reserved_stock == 10
    assert repository.audit_records == []


async def test_update_sku_allows_unlimited_without_committed_stock() -> None:
    repository = FakeAdminCatalogRepository()
    repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    repository.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="limited",
        status="active",
        currency="KRW",
        total_stock=100,
        reserved_stock=0,
        sold_stock=0,
    )

    result = await update_admin_one_time_sku(
        "product_reports",
        "sku_pack",
        AdminOneTimeSkuUpdateCommand(
            stock_policy={"type": "unlimited"},
            change_reason="remove stock limit before launch",
        ),
        AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        repository,
    )

    assert result.sku.stock_policy == "unlimited"
    assert result.sku.total_stock is None
    assert result.sku.reserved_stock is None
    assert result.sku.sold_stock is None
