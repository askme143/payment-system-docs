from __future__ import annotations

from payments.application.errors import ProviderError


def test_create_subscription_checkout_returns_sdk_inputs(client, auth_headers) -> None:
    response = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["subscriptionId"].startswith("sub_")
    assert body["customerKey"].startswith("pcus_key_")
    assert body["productCode"] == "basic"
    assert body["amount"] == 9900
    assert body["currency"] == "KRW"
    assert body["clientKey"] == "test_ck_local"
    assert body["successUrl"].startswith("https://example.com/subscription/success?")
    assert body["failUrl"].startswith("https://example.com/subscription/fail?")


def test_create_subscription_checkout_reuses_idempotent_response(
    client,
    auth_headers,
) -> None:
    headers = {**auth_headers, "Idempotency-Key": "subscription-checkout-key"}
    payload = {
        "planId": "plan_basic_monthly",
        "successUrl": "https://example.com/subscription/success",
        "failUrl": "https://example.com/subscription/fail",
    }

    first = client.post("/subscriptions/checkout", headers=headers, json=payload)
    second = client.post("/subscriptions/checkout", headers=headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()


def test_create_subscription_checkout_idempotency_conflict_returns_409(
    client,
    auth_headers,
) -> None:
    headers = {**auth_headers, "Idempotency-Key": "subscription-checkout-key"}
    first = client.post(
        "/subscriptions/checkout",
        headers=headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )
    second = client.post(
        "/subscriptions/checkout",
        headers=headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/other/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_create_subscription_checkout_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    invalid_payloads = [
        {},
        {
            "planId": 123,
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
        {
            "planId": "plan_basic_monthly",
            "successUrl": "not-a-url",
            "failUrl": "https://example.com/subscription/fail",
        },
        {
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "ftp://example.com/subscription/fail",
        },
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/subscriptions/checkout",
            headers=auth_headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_create_subscription_checkout_rejects_unavailable_plan_as_409(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.catalog_repository.plans["plan_basic_monthly"].status = "paused"

    response = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_confirm_subscription_checkout_returns_paid(client, auth_headers) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )

    response = client.post(
        "/subscriptions/confirm",
        headers={**auth_headers, "Idempotency-Key": "subscription-confirm-key"},
        json={
            "subscriptionId": checkout.json()["subscriptionId"],
            "customerKey": checkout.json()["customerKey"],
            "authKey": "auth_123",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionId"] == checkout.json()["subscriptionId"]
    assert body["status"] == "active"
    assert body["paymentStatus"] == "paid"
    assert body["paymentId"].startswith("pay_")
    assert body["invoiceId"].startswith("inv_")
    assert body["nextBillingDate"] == "2026-07-10"


def test_confirm_subscription_checkout_requires_idempotency_key(
    client,
    auth_headers,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )

    response = client.post(
        "/subscriptions/confirm",
        headers=auth_headers,
        json={
            "subscriptionId": checkout.json()["subscriptionId"],
            "customerKey": checkout.json()["customerKey"],
            "authKey": "auth_123",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_confirm_subscription_checkout_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )
    headers = {**auth_headers, "Idempotency-Key": "subscription-confirm-key"}
    invalid_payloads = [
        {},
        {
            "subscriptionId": 123,
            "customerKey": checkout.json()["customerKey"],
            "authKey": "auth_123",
        },
        {
            "subscriptionId": checkout.json()["subscriptionId"],
            "customerKey": "",
            "authKey": "auth_123",
        },
        {
            "subscriptionId": checkout.json()["subscriptionId"],
            "customerKey": checkout.json()["customerKey"],
            "authKey": None,
        },
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/subscriptions/confirm",
            headers=headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_confirm_subscription_checkout_returns_403_for_other_user(
    client,
    auth_headers,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )

    response = client.post(
        "/subscriptions/confirm",
        headers={
            **auth_headers,
            "X-Request-User-Id": "user_2",
            "Idempotency-Key": "subscription-confirm-key",
        },
        json={
            "subscriptionId": checkout.json()["subscriptionId"],
            "customerKey": checkout.json()["customerKey"],
            "authKey": "auth_123",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_confirm_subscription_checkout_reuses_idempotent_response(
    client,
    auth_headers,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )
    payload = {
        "subscriptionId": checkout.json()["subscriptionId"],
        "customerKey": checkout.json()["customerKey"],
        "authKey": "auth_123",
    }
    headers = {**auth_headers, "Idempotency-Key": "subscription-confirm-key"}

    first = client.post("/subscriptions/confirm", headers=headers, json=payload)
    second = client.post("/subscriptions/confirm", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


def test_confirm_subscription_checkout_reuses_subscription_success(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )
    payload = {
        "subscriptionId": checkout.json()["subscriptionId"],
        "customerKey": checkout.json()["customerKey"],
        "authKey": "auth_123",
    }

    first = client.post(
        "/subscriptions/confirm",
        headers={**auth_headers, "Idempotency-Key": "subscription-confirm-key-1"},
        json=payload,
    )
    second = client.post(
        "/subscriptions/confirm",
        headers={**auth_headers, "Idempotency-Key": "subscription-confirm-key-2"},
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 1
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


def test_confirm_subscription_checkout_billing_key_failure_returns_402(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )
    test_dependencies.payment_provider.issue_billing_key_error = ProviderError(
        "인증 시간이 만료되었습니다.",
        provider_code="INVALID_AUTH_KEY",
    )
    payload = {
        "subscriptionId": checkout.json()["subscriptionId"],
        "customerKey": checkout.json()["customerKey"],
        "authKey": "auth_123",
    }
    headers = {**auth_headers, "Idempotency-Key": "subscription-confirm-key"}

    first = client.post("/subscriptions/confirm", headers=headers, json=payload)
    second = client.post("/subscriptions/confirm", headers=headers, json=payload)

    assert first.status_code == 402
    assert first.json() == {
        "subscriptionId": checkout.json()["subscriptionId"],
        "status": "pending",
        "failure": {
            "code": "BILLING_KEY_ISSUE_FAILED",
            "providerCode": "INVALID_AUTH_KEY",
            "message": "인증 시간이 만료되었습니다.",
            "retryable": True,
        },
    }
    assert second.status_code == 402
    assert second.json() == first.json()


def test_confirm_subscription_checkout_first_payment_failure_returns_402(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "잔액 부족",
        provider_code="INSUFFICIENT_FUNDS",
    )
    payload = {
        "subscriptionId": checkout.json()["subscriptionId"],
        "customerKey": checkout.json()["customerKey"],
        "authKey": "auth_123",
    }
    headers = {**auth_headers, "Idempotency-Key": "subscription-confirm-key"}

    first = client.post("/subscriptions/confirm", headers=headers, json=payload)
    second = client.post("/subscriptions/confirm", headers=headers, json=payload)

    assert first.status_code == 402
    body = first.json()
    assert body["subscriptionId"] == checkout.json()["subscriptionId"]
    assert body["status"] == "pending"
    assert body["paymentStatus"] == "failed"
    assert body["paymentId"].startswith("pay_")
    assert body["invoiceId"].startswith("inv_")
    assert body["failure"] == {
        "code": "FIRST_PAYMENT_FAILED",
        "providerCode": "INSUFFICIENT_FUNDS",
        "message": "잔액 부족",
        "retryable": True,
    }
    assert second.status_code == 402
    assert second.json() == first.json()


def test_confirm_subscription_checkout_idempotency_conflict_returns_409(
    client,
    auth_headers,
) -> None:
    checkout = client.post(
        "/subscriptions/checkout",
        headers=auth_headers,
        json={
            "planId": "plan_basic_monthly",
            "successUrl": "https://example.com/subscription/success",
            "failUrl": "https://example.com/subscription/fail",
        },
    )
    headers = {**auth_headers, "Idempotency-Key": "subscription-confirm-key"}
    first = client.post(
        "/subscriptions/confirm",
        headers=headers,
        json={
            "subscriptionId": checkout.json()["subscriptionId"],
            "customerKey": checkout.json()["customerKey"],
            "authKey": "auth_123",
        },
    )
    second = client.post(
        "/subscriptions/confirm",
        headers=headers,
        json={
            "subscriptionId": checkout.json()["subscriptionId"],
            "customerKey": checkout.json()["customerKey"],
            "authKey": "auth_other",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"
