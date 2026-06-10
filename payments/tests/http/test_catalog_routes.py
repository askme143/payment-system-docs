from __future__ import annotations


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
        "plans": [
            {
                "id": "plan_basic_monthly",
                "productId": "product_basic",
                "productCode": "basic",
                "name": "Basic",
                "planCode": "basic_monthly",
                "billingPeriod": "monthly",
                "amount": 9900,
                "entitlements": {"seats": 1},
                "status": "active",
            }
        ]
    }


def test_get_missing_plan_returns_404(client, auth_headers) -> None:
    response = client.get("/plans/missing", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
