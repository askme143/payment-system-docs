from __future__ import annotations

from datetime import UTC, datetime

from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


def test_health_returns_ok(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_list_plans_requires_internal_authorization(client) -> None:
    response = client.get("/plans", headers={"X-Request-Id": "req_test"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_list_plans_requires_request_id(client) -> None:
    response = client.get("/plans", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_list_plans_returns_active_catalog(client, auth_headers) -> None:
    response = client.get("/plans", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "productId": "product_basic",
                "productCode": "basic",
                "productName": "Basic",
                "plans": [
                    {
                        "planId": "plan_basic_monthly",
                        "planCode": "basic_monthly",
                        "name": "Basic 월간",
                        "billingPeriod": "monthly",
                        "amount": 9900,
                        "currency": "KRW",
                        "status": "active",
                        "isPurchasable": True,
                        "entitlements": ["seats"],
                        "detailUrl": "/plans/plan_basic_monthly",
                    }
                ],
            }
        ]
    }


def test_list_plans_filters_by_product_and_billing_period(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.catalog_repository.products["product_pro"] = Product(
        id="product_pro",
        product_code="pro",
        product_type="subscription",
        name="Pro",
        status="active",
    )
    test_dependencies.catalog_repository.plans["plan_pro_yearly"] = (
        SubscriptionPlan(
            id="plan_pro_yearly",
            product_id="product_pro",
            plan_code="pro_yearly",
            billing_period="yearly",
            amount=99000,
            entitlements={"priority_support": True},
            status="active",
        )
    )

    response = client.get(
        "/plans?productCode=pro&billingPeriod=yearly&includeUnavailable=false",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["productCode"] == "pro"
    assert body["items"][0]["plans"][0]["planId"] == "plan_pro_yearly"


def test_list_plans_hides_unavailable_current_plan_by_default(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
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

    response = client.get("/plans", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_plans_hides_same_product_plans_by_default(
    client,
    auth_headers,
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

    response = client.get("/plans", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_plans_includes_unavailable_reason_when_requested(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
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

    response = client.get(
        "/plans?includeUnavailable=true",
        headers=auth_headers,
    )

    assert response.status_code == 200
    plan = response.json()["items"][0]["plans"][0]
    assert plan["isPurchasable"] is False
    assert plan["unavailableReason"] == "PRODUCT_ALREADY_SUBSCRIBED"


def test_get_plan_returns_409_for_same_product_plan(
    client,
    auth_headers,
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

    response = client.get("/plans/plan_basic_yearly", headers=auth_headers)

    assert response.status_code == 409
    assert response.json() == {
        "planId": "plan_basic_yearly",
        "isPurchasable": False,
        "unavailableReason": "PRODUCT_ALREADY_SUBSCRIBED",
    }


def test_list_plans_rejects_invalid_query_values_as_400(client, auth_headers) -> None:
    invalid_urls = [
        "/plans?billingPeriod=weekly",
        "/plans?includeUnavailable=maybe",
        "/plans?productCode=",
    ]

    for url in invalid_urls:
        response = client.get(url, headers=auth_headers)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_get_plan_returns_documented_detail_shape(client, auth_headers) -> None:
    response = client.get("/plans/plan_basic_monthly", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "productId": "product_basic",
        "productCode": "basic",
        "productName": "Basic",
        "planId": "plan_basic_monthly",
        "planCode": "basic_monthly",
        "name": "Basic 월간",
        "description": None,
        "billingPeriod": "monthly",
        "amount": 9900,
        "currency": "KRW",
        "status": "active",
        "isPurchasable": True,
        "unavailableReason": None,
        "entitlements": [{"code": "seats", "label": "Seats"}],
        "checkoutUrl": "/subscriptions/checkout",
    }


def test_get_plan_returns_409_when_current_user_cannot_select_plan(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
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

    response = client.get("/plans/plan_basic_monthly", headers=auth_headers)

    assert response.status_code == 409
    assert response.json() == {
        "planId": "plan_basic_monthly",
        "isPurchasable": False,
        "unavailableReason": "PRODUCT_ALREADY_SUBSCRIBED",
    }


def test_get_missing_plan_returns_404(client, auth_headers) -> None:
    response = client.get("/plans/missing", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
