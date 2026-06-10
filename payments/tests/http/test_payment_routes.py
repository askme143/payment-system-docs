from __future__ import annotations


def order_payload(quantity: int = 2) -> dict:
    return {
        "items": [{"skuId": "sku_report_pack_100", "quantity": quantity}],
        "successUrl": "https://example.com/payments/success",
        "failUrl": "https://example.com/payments/fail",
    }


def test_create_payment_order_requires_user(client) -> None:
    response = client.post(
        "/payments/orders",
        headers={
            "Authorization": "Bearer secret",
            "X-Request-Id": "req_test",
        },
        json=order_payload(),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_create_payment_order_returns_ready_payment(client, auth_headers) -> None:
    response = client.post(
        "/payments/orders", headers=auth_headers, json=order_payload()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["checkoutId"].startswith("chk_")
    assert body["paymentId"].startswith("pay_")
    assert body["orderId"].startswith("order_")
    assert body["amount"] == 2000
    assert body["status"] == "ready"


def test_create_payment_order_requires_sku_id(client, auth_headers) -> None:
    payload = {
        **order_payload(),
        "items": [{"productId": "prod_report_pack", "quantity": 1}],
    }

    response = client.post("/payments/orders", headers=auth_headers, json=payload)

    assert response.status_code == 422


def test_create_payment_order_reuses_idempotent_response(client, auth_headers) -> None:
    headers = {**auth_headers, "Idempotency-Key": "same-key"}

    first = client.post("/payments/orders", headers=headers, json=order_payload())
    second = client.post("/payments/orders", headers=headers, json=order_payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


def test_idempotency_key_conflict_returns_409(client, auth_headers) -> None:
    headers = {**auth_headers, "Idempotency-Key": "same-key"}

    first = client.post("/payments/orders", headers=headers, json=order_payload())
    second = client.post("/payments/orders", headers=headers, json=order_payload(3))

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_get_payment_detail_enforces_ownership(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders", headers=auth_headers, json=order_payload()
    )
    payment_id = created.json()["paymentId"]

    owned = client.get(f"/payments/{payment_id}", headers=auth_headers)
    other_user = client.get(
        f"/payments/{payment_id}",
        headers={**auth_headers, "X-Request-User-Id": "user_2"},
    )

    assert owned.status_code == 200
    assert owned.json()["id"] == payment_id
    assert other_user.status_code == 404
    assert other_user.json()["error"]["code"] == "not_found"
