from __future__ import annotations

import pytest

from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan


def test_create_admin_product_requires_admin_context(client, auth_headers) -> None:
    response = client.post(
        "/admin/products",
        headers=auth_headers,
        json={
            "productCode": "ANALYTICS",
            "productType": "subscription",
            "name": "Analytics",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_create_admin_product_returns_created_product(client, admin_headers) -> None:
    response = client.post(
        "/admin/products",
        headers=admin_headers,
        json={
            "productCode": "ANALYTICS",
            "productType": "subscription",
            "name": "Analytics",
            "description": "analytics subscription product",
            "displayOrder": 10,
            "status": "draft",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["productId"].startswith("product_")
    assert body["productCode"] == "ANALYTICS"
    assert body["productType"] == "subscription"
    assert body["status"] == "draft"
    assert body["subscriptionPlans"] == []
    assert body["oneTimeSkus"] == []


def test_create_admin_product_allows_same_code_for_different_type(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_subscription"] = Product(
        id="product_subscription",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics subscription",
        status="draft",
    )

    response = client.post(
        "/admin/products",
        headers=admin_headers,
        json={
            "productCode": "ANALYTICS",
            "productType": "one_time",
            "name": "Analytics reports",
            "status": "draft",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["productCode"] == "ANALYTICS"
    assert body["productType"] == "one_time"


def test_create_admin_product_rejects_invalid_product_code(
    client,
    admin_headers,
) -> None:
    response = client.post(
        "/admin/products",
        headers=admin_headers,
        json={
            "productCode": "ANALYTICS-PRO",
            "productType": "subscription",
            "name": "Analytics",
            "status": "draft",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_create_admin_product_rejects_schema_validation_as_400(
    client,
    admin_headers,
) -> None:
    response = client.post(
        "/admin/products",
        headers=admin_headers,
        json={
            "productCode": "ANALYTICS",
            "productType": "rental",
            "name": "Analytics",
            "status": "draft",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_create_admin_product_rejects_active_initial_status(
    client,
    admin_headers,
) -> None:
    response = client.post(
        "/admin/products",
        headers=admin_headers,
        json={
            "productCode": "ANALYTICS",
            "productType": "subscription",
            "name": "Analytics",
            "status": "active",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_change_admin_product_status_returns_transition_result(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = (
        Product(
            id="product_analytics",
            product_code="ANALYTICS",
            product_type="subscription",
            name="Analytics",
            status="draft",
        )
    )
    test_dependencies.admin_catalog.active_subscription_plan_counts[
        "product_analytics"
    ] = 1

    response = client.patch(
        "/admin/products/product_analytics/status",
        headers=admin_headers,
        json={
            "status": "active",
            "reason": "launch approved",
            "effectiveAt": "2026-06-08T10:00:00+09:00",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "productId": "product_analytics",
        "productCode": "ANALYTICS",
        "productType": "subscription",
        "previousStatus": "draft",
        "status": "active",
        "effectiveAt": "2026-06-08T10:00:00+09:00",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": "missing status"},
        {"status": "deleted", "reason": "invalid status"},
        {"status": "paused"},
        {"status": "paused", "reason": " "},
        {
            "status": "paused",
            "reason": "incident response",
            "effectiveAt": "not-a-date",
        },
        {
            "status": "paused",
            "reason": "incident response",
            "effectiveAt": "2026-06-08T10:00:00",
        },
        {
            "status": "paused",
            "reason": "incident response",
            "effectiveAt": 123,
        },
    ],
)
def test_change_admin_product_status_rejects_invalid_contract_values(
    client,
    admin_headers,
    test_dependencies,
    payload,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="active",
    )

    response = client.patch(
        "/admin/products/product_analytics/status",
        headers=admin_headers,
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_change_admin_product_status_returns_404_for_missing_product(
    client,
    admin_headers,
) -> None:
    response = client.patch(
        "/admin/products/missing_product/status",
        headers=admin_headers,
        json={
            "status": "paused",
            "reason": "maintenance",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_change_admin_product_status_rejects_active_without_selling_unit(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    response = client.patch(
        "/admin/products/product_analytics/status",
        headers=admin_headers,
        json={
            "status": "active",
            "reason": "launch approved",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_change_admin_product_status_rejects_archived_reactivation(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="archived",
    )
    test_dependencies.admin_catalog.active_subscription_plan_counts[
        "product_analytics"
    ] = 1

    response = client.patch(
        "/admin/products/product_analytics/status",
        headers=admin_headers,
        json={
            "status": "active",
            "reason": "reactivate",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_change_admin_product_status_rejects_return_to_draft(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="active",
    )

    response = client.patch(
        "/admin/products/product_analytics/status",
        headers=admin_headers,
        json={
            "status": "draft",
            "reason": "undo launch",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_create_admin_subscription_plan_returns_created_plan(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    response = client.post(
        "/admin/products/product_analytics/subscription-plans",
        headers=admin_headers,
        json={
            "planCode": "ANALYTICS_BASIC_MONTHLY",
            "planName": "Analytics Basic Monthly",
            "billingPeriod": "monthly",
            "amount": 9900,
            "currency": "KRW",
            "status": "active",
            "entitlements": {"seatLimit": 3},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["productId"] == "product_analytics"
    assert body["productType"] == "subscription"
    assert body["planId"].startswith("plan_")
    assert body["status"] == "active"
    assert body["billingPeriod"] == "monthly"
    assert body["amount"] == 9900
    assert body["currency"] == "KRW"


def test_create_admin_subscription_plan_rejects_invalid_amount(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    response = client.post(
        "/admin/products/product_analytics/subscription-plans",
        headers=admin_headers,
        json={
            "planCode": "ANALYTICS_BASIC_MONTHLY",
            "billingPeriod": "monthly",
            "amount": 0,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_create_admin_subscription_plan_rejects_invalid_contract_values(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    payloads = [
        {
            "planCode": "ANALYTICS-BASIC-MONTHLY",
            "billingPeriod": "monthly",
            "amount": 9900,
        },
        {
            "planCode": "ANALYTICS_BASIC_MONTHLY",
            "billingPeriod": "weekly",
            "amount": 9900,
        },
        {
            "planCode": "ANALYTICS_BASIC_MONTHLY",
            "billingPeriod": "monthly",
            "amount": "9900",
        },
        {
            "planCode": "ANALYTICS_BASIC_MONTHLY",
            "billingPeriod": "monthly",
            "amount": 9900,
            "currency": "USD",
        },
        {
            "planCode": "ANALYTICS_BASIC_MONTHLY",
            "billingPeriod": "monthly",
            "amount": 9900,
            "entitlements": ["seatLimit"],
        },
    ]

    for payload in payloads:
        response = client.post(
            "/admin/products/product_analytics/subscription-plans",
            headers=admin_headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_create_admin_subscription_plan_rejects_duplicate_plan_code(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    test_dependencies.admin_catalog.subscription_plans["plan_basic"] = (
        SubscriptionPlan(
            id="plan_basic",
            product_id="product_analytics",
            plan_code="ANALYTICS_BASIC_MONTHLY",
            billing_period="monthly",
            amount=9900,
            entitlements={},
            status="active",
        )
    )

    response = client.post(
        "/admin/products/product_analytics/subscription-plans",
        headers=admin_headers,
        json={
            "planCode": "ANALYTICS_BASIC_MONTHLY",
            "billingPeriod": "monthly",
            "amount": 12900,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_create_admin_subscription_plan_rejects_one_time_product(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    response = client.post(
        "/admin/products/product_reports/subscription-plans",
        headers=admin_headers,
        json={
            "planCode": "REPORTS_BASIC_MONTHLY",
            "billingPeriod": "monthly",
            "amount": 9900,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_update_admin_subscription_plan_returns_updated_policy(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    test_dependencies.admin_catalog.subscription_plans["plan_basic"] = (
        SubscriptionPlan(
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
    )

    response = client.patch(
        "/admin/products/product_analytics/subscription-plans/plan_basic",
        headers=admin_headers,
        json={
            "amount": 12900,
            "currency": "KRW",
            "status": "active",
            "entitlements": {"seatLimit": 5},
            "changeReason": "seat and price adjustment",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "productId": "product_analytics",
        "productType": "subscription",
        "planId": "plan_basic",
        "status": "active",
        "amount": 12900,
        "currency": "KRW",
        "version": 2,
        "effectiveFor": "new_subscriptions_and_next_cycles",
    }


def test_update_admin_subscription_plan_rejects_empty_update(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    test_dependencies.admin_catalog.subscription_plans["plan_basic"] = (
        SubscriptionPlan(
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
    )

    response = client.patch(
        "/admin/products/product_analytics/subscription-plans/plan_basic",
        headers=admin_headers,
        json={"changeReason": "audit only is not a policy change"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_update_admin_subscription_plan_rejects_invalid_contract_values(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )
    test_dependencies.admin_catalog.subscription_plans["plan_basic"] = (
        SubscriptionPlan(
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
    )

    payloads = [
        {"amount": 0, "changeReason": "invalid amount"},
        {"amount": "12900", "changeReason": "invalid amount type"},
        {"currency": "USD", "changeReason": "invalid currency"},
        {"status": "deleted", "changeReason": "invalid status"},
        {"entitlements": ["seatLimit"], "changeReason": "invalid entitlements"},
    ]

    for payload in payloads:
        response = client.patch(
            "/admin/products/product_analytics/subscription-plans/plan_basic",
            headers=admin_headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_update_admin_subscription_plan_returns_404_for_missing_plan(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    response = client.patch(
        "/admin/products/product_analytics/subscription-plans/missing_plan",
        headers=admin_headers,
        json={
            "amount": 12900,
            "changeReason": "price adjustment",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_update_admin_subscription_plan_rejects_one_time_product(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    response = client.patch(
        "/admin/products/product_reports/subscription-plans/plan_basic",
        headers=admin_headers,
        json={
            "amount": 12900,
            "changeReason": "price adjustment",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_create_admin_one_time_sku_returns_created_sku(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    response = client.post(
        "/admin/products/product_reports/one-time-skus",
        headers=admin_headers,
        json={
            "skuCode": "REPORT_PACK_100",
            "skuName": "Report pack 100",
            "amount": 50000,
            "currency": "KRW",
            "status": "active",
            "stockPolicy": {"type": "unlimited"},
            "purchaseLimit": {"perUser": 5},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["productId"] == "product_reports"
    assert body["productType"] == "one_time"
    assert body["skuId"].startswith("sku_")
    assert body["status"] == "active"
    assert body["amount"] == 50000
    assert body["currency"] == "KRW"
    assert body["stockPolicy"] == {"type": "unlimited"}


def test_create_admin_one_time_sku_rejects_invalid_limited_stock(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    response = client.post(
        "/admin/products/product_reports/one-time-skus",
        headers=admin_headers,
        json={
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
            "stockPolicy": {"type": "limited"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_create_admin_one_time_sku_rejects_invalid_contract_values(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    payloads = [
        {"skuCode": "REPORT-PACK-100", "amount": 50000},
        {"skuCode": "REPORT_PACK_100", "amount": "50000"},
        {"skuCode": "REPORT_PACK_100", "amount": 0},
        {"skuCode": "REPORT_PACK_100", "amount": 50000, "currency": "USD"},
        {"skuCode": "REPORT_PACK_100", "amount": 50000, "status": "deleted"},
        {
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
            "stockPolicy": {"type": "external"},
        },
        {
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
            "stockPolicy": "limited",
        },
        {
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
            "stockPolicy": {"type": "limited"},
            "totalStock": "800",
        },
        {
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
            "stockPolicy": {"type": "unlimited"},
            "totalStock": 800,
        },
        {
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
            "purchaseLimit": {"perUser": 0},
        },
        {
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
            "purchaseLimit": ["perUser"],
        },
    ]

    for payload in payloads:
        response = client.post(
            "/admin/products/product_reports/one-time-skus",
            headers=admin_headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_create_admin_one_time_sku_rejects_duplicate_sku_code(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    test_dependencies.admin_catalog.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="unlimited",
        status="active",
    )

    response = client.post(
        "/admin/products/product_reports/one-time-skus",
        headers=admin_headers,
        json={
            "skuCode": "REPORT_PACK_100",
            "amount": 60000,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_create_admin_one_time_sku_returns_404_for_missing_product(
    client,
    admin_headers,
) -> None:
    response = client.post(
        "/admin/products/missing_product/one-time-skus",
        headers=admin_headers,
        json={
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_create_admin_one_time_sku_rejects_subscription_product(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    response = client.post(
        "/admin/products/product_analytics/one-time-skus",
        headers=admin_headers,
        json={
            "skuCode": "REPORT_PACK_100",
            "amount": 50000,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_update_admin_one_time_sku_returns_updated_policy(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    test_dependencies.admin_catalog.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="unlimited",
        status="active",
        currency="KRW",
    )

    response = client.patch(
        "/admin/products/product_reports/one-time-skus/sku_pack",
        headers=admin_headers,
        json={
            "amount": 55000,
            "currency": "KRW",
            "status": "active",
            "stockPolicy": {"type": "limited"},
            "totalStock": 800,
            "purchaseLimit": {"perUser": 3},
            "changeReason": "promotion ended",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "productId": "product_reports",
        "productType": "one_time",
        "skuId": "sku_pack",
        "status": "active",
        "amount": 55000,
        "currency": "KRW",
        "stockPolicy": {"type": "limited"},
        "totalStock": 800,
        "reservedStock": 0,
        "soldStock": 0,
        "availableStock": 800,
        "effectiveFor": "new_orders",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": 0, "changeReason": "invalid amount"},
        {"amount": "55000", "changeReason": "invalid amount"},
        {"currency": "USD", "changeReason": "invalid currency"},
        {"status": "deleted", "changeReason": "invalid status"},
        {"stockPolicy": {"type": "external"}, "changeReason": "invalid stock"},
        {"stockPolicy": "limited", "changeReason": "invalid stock"},
        {"totalStock": "800", "changeReason": "invalid stock"},
        {"totalStock": 800, "changeReason": "invalid stock"},
        {"purchaseLimit": {"perOrder": False}, "changeReason": "invalid limit"},
        {"purchaseLimit": ["perOrder"], "changeReason": "invalid limit"},
    ],
)
def test_update_admin_one_time_sku_rejects_invalid_contract_values(
    client,
    admin_headers,
    test_dependencies,
    payload,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    test_dependencies.admin_catalog.one_time_skus["sku_pack"] = OneTimeSku(
        id="sku_pack",
        product_id="product_reports",
        sku_code="REPORT_PACK_100",
        amount=50000,
        stock_policy="unlimited",
        status="active",
        currency="KRW",
    )

    response = client.patch(
        "/admin/products/product_reports/one-time-skus/sku_pack",
        headers=admin_headers,
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_update_admin_one_time_sku_returns_404_for_missing_sku(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )

    response = client.patch(
        "/admin/products/product_reports/one-time-skus/missing_sku",
        headers=admin_headers,
        json={
            "amount": 55000,
            "changeReason": "price correction",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_update_admin_one_time_sku_rejects_subscription_product(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_analytics"] = Product(
        id="product_analytics",
        product_code="ANALYTICS",
        product_type="subscription",
        name="Analytics",
        status="draft",
    )

    response = client.patch(
        "/admin/products/product_analytics/one-time-skus/sku_pack",
        headers=admin_headers,
        json={
            "amount": 55000,
            "changeReason": "wrong API",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_update_admin_one_time_sku_rejects_total_stock_below_committed_stock(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    test_dependencies.admin_catalog.one_time_skus["sku_pack"] = OneTimeSku(
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

    response = client.patch(
        "/admin/products/product_reports/one-time-skus/sku_pack",
        headers=admin_headers,
        json={
            "totalStock": 60,
            "changeReason": "over-tighten stock",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_update_admin_one_time_sku_rejects_unlimited_transition_with_committed_stock(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_catalog.products["product_reports"] = Product(
        id="product_reports",
        product_code="REPORTS",
        product_type="one_time",
        name="Reports",
        status="draft",
    )
    test_dependencies.admin_catalog.one_time_skus["sku_pack"] = OneTimeSku(
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

    response = client.patch(
        "/admin/products/product_reports/one-time-skus/sku_pack",
        headers=admin_headers,
        json={
            "stockPolicy": {"type": "unlimited"},
            "changeReason": "remove stock limit",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
