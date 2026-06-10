from __future__ import annotations

import pytest

from payments.application.catalog import get_subscription_plan, list_subscription_plans
from payments.application.errors import ResourceNotFoundError


async def test_list_subscription_plans_returns_active_catalog(
    test_dependencies,
) -> None:
    plans = await list_subscription_plans(test_dependencies.catalog_repository)

    assert len(plans) == 1
    assert plans[0].id == "plan_basic_monthly"
    assert plans[0].product_code == "basic"


async def test_get_subscription_plan_returns_detail(test_dependencies) -> None:
    plan = await get_subscription_plan(
        "plan_basic_monthly",
        test_dependencies.catalog_repository,
    )

    assert plan.id == "plan_basic_monthly"
    assert plan.amount == 9900


async def test_get_subscription_plan_raises_for_missing_plan(test_dependencies) -> None:
    with pytest.raises(ResourceNotFoundError):
        await get_subscription_plan("missing", test_dependencies.catalog_repository)
