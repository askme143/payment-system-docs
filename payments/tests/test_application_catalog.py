from __future__ import annotations

from datetime import UTC, datetime

import pytest

from payments.application.catalog import get_subscription_plan, list_subscription_plans
from payments.application.errors import ConflictResponseError, ResourceNotFoundError
from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


async def test_list_subscription_plans_returns_active_catalog(
    test_dependencies,
) -> None:
    plans = await list_subscription_plans(test_dependencies.catalog_repository)

    assert len(plans) == 1
    assert plans[0].id == "plan_basic_monthly"
    assert plans[0].product_code == "basic"
    assert plans[0].name == "Basic 월간"


async def test_list_subscription_plans_excludes_one_time_products(
    test_dependencies,
) -> None:
    test_dependencies.catalog_repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="reports",
        product_type="one_time",
        name="Reports",
        status="active",
    )
    test_dependencies.catalog_repository.plans["plan_reports_monthly"] = (
        SubscriptionPlan(
            id="plan_reports_monthly",
            product_id="product_reports",
            plan_code="reports_monthly",
            billing_period="monthly",
            amount=4900,
            entitlements={"report_pack": True},
            status="active",
        )
    )

    plans = await list_subscription_plans(test_dependencies.catalog_repository)

    assert [plan.id for plan in plans] == ["plan_basic_monthly"]


async def test_get_subscription_plan_excludes_one_time_product_plan(
    test_dependencies,
) -> None:
    test_dependencies.catalog_repository.products["product_reports"] = Product(
        id="product_reports",
        product_code="reports",
        product_type="one_time",
        name="Reports",
        status="active",
    )
    test_dependencies.catalog_repository.plans["plan_reports_monthly"] = (
        SubscriptionPlan(
            id="plan_reports_monthly",
            product_id="product_reports",
            plan_code="reports_monthly",
            billing_period="monthly",
            amount=4900,
            entitlements={"report_pack": True},
            status="active",
        )
    )

    with pytest.raises(ResourceNotFoundError):
        await get_subscription_plan(
            "plan_reports_monthly",
            test_dependencies.catalog_repository,
        )


async def test_list_subscription_plans_applies_stable_display_order(
    test_dependencies,
) -> None:
    basic = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_basic_yearly"] = (
        SubscriptionPlan(
            id="plan_basic_yearly",
            product_id=basic.id,
            plan_code="basic_yearly",
            billing_period="yearly",
            amount=99000,
            entitlements={"seats": 1},
            status="active",
        )
    )
    test_dependencies.catalog_repository.products["product_advanced"] = Product(
        id="product_advanced",
        product_code="advanced",
        product_type="subscription",
        name="Advanced",
        status="active",
    )
    test_dependencies.catalog_repository.plans["plan_advanced_monthly"] = (
        SubscriptionPlan(
            id="plan_advanced_monthly",
            product_id="product_advanced",
            plan_code="advanced_monthly",
            billing_period="monthly",
            amount=19900,
            entitlements={"seats": 3},
            status="active",
        )
    )

    plans = await list_subscription_plans(test_dependencies.catalog_repository)

    assert [plan.id for plan in plans] == [
        "plan_advanced_monthly",
        "plan_basic_monthly",
        "plan_basic_yearly",
    ]


async def test_get_subscription_plan_returns_detail(test_dependencies) -> None:
    plan = await get_subscription_plan(
        "plan_basic_monthly",
        test_dependencies.catalog_repository,
    )

    assert plan.id == "plan_basic_monthly"
    assert plan.amount == 9900
    assert plan.name == "Basic 월간"


async def test_list_subscription_plans_hides_subscribed_product_by_default(
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_basic_yearly"] = (
        SubscriptionPlan(
            id="plan_basic_yearly",
            product_id=product.id,
            plan_code="basic_yearly",
            billing_period="yearly",
            amount=99000,
            entitlements={"seats": 1},
            status="active",
        )
    )
    test_dependencies.catalog_repository.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    plans = await list_subscription_plans(
        test_dependencies.catalog_repository,
        user_id="user_1",
    )

    assert plans == []


async def test_list_subscription_plans_includes_unavailable_subscribed_product(
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_basic_yearly"] = (
        SubscriptionPlan(
            id="plan_basic_yearly",
            product_id=product.id,
            plan_code="basic_yearly",
            billing_period="yearly",
            amount=99000,
            entitlements={"seats": 1},
            status="active",
        )
    )
    test_dependencies.catalog_repository.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    plans = await list_subscription_plans(
        test_dependencies.catalog_repository,
        include_unavailable=True,
        user_id="user_1",
    )

    by_id = {plan.id: plan for plan in plans}
    assert by_id["plan_basic_monthly"].is_purchasable is False
    assert (
        by_id["plan_basic_monthly"].unavailable_reason
        == "PRODUCT_ALREADY_SUBSCRIBED"
    )
    assert by_id["plan_basic_yearly"].is_purchasable is False
    assert (
        by_id["plan_basic_yearly"].unavailable_reason
        == "PRODUCT_ALREADY_SUBSCRIBED"
    )


async def test_get_subscription_plan_raises_conflict_for_current_plan(
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="cancel_scheduled",
        cancel_at_period_end=True,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        access_until=datetime(2026, 7, 1, tzinfo=UTC),
    )

    with pytest.raises(ConflictResponseError) as exc_info:
        await get_subscription_plan(
            "plan_basic_monthly",
            test_dependencies.catalog_repository,
            user_id="user_1",
        )

    assert exc_info.value.response_body == {
        "planId": "plan_basic_monthly",
        "isPurchasable": False,
        "unavailableReason": "PRODUCT_ALREADY_SUBSCRIBED",
    }


async def test_get_subscription_plan_raises_conflict_for_same_product_plan(
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_basic_yearly"] = (
        SubscriptionPlan(
            id="plan_basic_yearly",
            product_id=product.id,
            plan_code="basic_yearly",
            billing_period="yearly",
            amount=99000,
            entitlements={"seats": 1},
            status="active",
        )
    )
    test_dependencies.catalog_repository.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    with pytest.raises(ConflictResponseError) as exc_info:
        await get_subscription_plan(
            "plan_basic_yearly",
            test_dependencies.catalog_repository,
            user_id="user_1",
        )

    assert exc_info.value.response_body == {
        "planId": "plan_basic_yearly",
        "isPurchasable": False,
        "unavailableReason": "PRODUCT_ALREADY_SUBSCRIBED",
    }


async def test_get_subscription_plan_raises_for_missing_plan(test_dependencies) -> None:
    with pytest.raises(ResourceNotFoundError):
        await get_subscription_plan("missing", test_dependencies.catalog_repository)
