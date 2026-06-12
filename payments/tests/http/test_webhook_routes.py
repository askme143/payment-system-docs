from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from payments.http.composition import create_app


def test_receive_toss_payment_webhook_returns_received(
    client,
    test_dependencies,
) -> None:
    response = client.post(
        "/webhooks/toss-payments",
        headers={"Toss-Signature": "webhook-secret"},
        json={
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_route_123",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
            "approvedAt": "2026-06-10T00:01:00+00:00",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert test_dependencies.webhook_uow_factory.enter_count == 1
    assert test_dependencies.webhook_uow_factory.commit_count == 1


def test_receive_toss_payment_webhook_rejects_missing_signature(client) -> None:
    response = client.post(
        "/webhooks/toss-payments",
        json={
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_route_123",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
        },
    )

    assert response.status_code == 401


def test_receive_toss_payment_webhook_rejects_invalid_signature(
    test_dependencies,
) -> None:
    app = create_app(
        replace(
            test_dependencies.to_http_dependencies(),
            toss_webhook_secret="webhook-secret",
        )
    )

    response = TestClient(app).post(
        "/webhooks/toss-payments",
        headers={"Toss-Signature": "wrong"},
        json={
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_route_123",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
        },
    )

    assert response.status_code == 401
