from __future__ import annotations

from datetime import UTC, datetime

from payments.application.errors import ProviderError


def test_start_billing_auth_requires_user(client) -> None:
    response = client.post(
        "/billing/auth",
        headers={
            "Authorization": "Bearer secret",
            "X-Request-Id": "req_test",
        },
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_start_billing_auth_returns_toss_sdk_inputs(client, auth_headers) -> None:
    response = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["billingAuthId"].startswith("bauth_")
    assert body["customerKey"].startswith("pcus_key_")
    assert body["clientKey"] == "test_ck_local"
    assert body["successUrl"].startswith("https://example.com/success?")
    assert body["failUrl"].startswith("https://example.com/fail?")
    assert body["setAsDefault"] is True
    assert body["status"] == "ready"


def test_start_billing_auth_rejects_unallowed_redirect_host(
    client,
    auth_headers,
) -> None:
    response = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://evil.example.net/success",
            "failUrl": "https://example.com/fail",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_start_billing_auth_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    invalid_payloads = [
        {"failUrl": "https://example.com/fail"},
        {"successUrl": 123, "failUrl": "https://example.com/fail"},
        {"successUrl": "https://example.com/success", "failUrl": 123},
        {
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": "true",
        },
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/billing/auth",
            headers=auth_headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_issue_billing_key_returns_billing_method(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )

    response = client.post(
        "/billing/issue",
        headers={**auth_headers, "Idempotency-Key": "billing-issue-key"},
        json={
            "billingAuthId": started.json()["billingAuthId"],
            "authKey": "auth_123",
            "customerKey": started.json()["customerKey"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["billingMethodId"].startswith("bm_")
    assert body["status"] == "active"
    assert body["isDefault"] is True
    assert body["method"] == "카드"
    assert body["cardCompany"] == "현대"
    assert body["maskedCardNumber"] == "**** **** **** 1234"
    assert body["billingKeyStatus"] == "active"
    assert body["createdAt"] == "2026-06-10T00:00:00Z"
    assert test_dependencies.billing_auth_issue_uow_factory.enter_count == 1
    assert test_dependencies.billing_auth_issue_uow_factory.commit_count == 1


def test_issue_billing_key_requires_idempotency_key(client, auth_headers) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )

    response = client.post(
        "/billing/issue",
        headers=auth_headers,
        json={
            "billingAuthId": started.json()["billingAuthId"],
            "authKey": "auth_123",
            "customerKey": started.json()["customerKey"],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_issue_billing_key_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )

    invalid_payloads = [
        {},
        {
            "billingAuthId": 123,
            "authKey": "auth_123",
            "customerKey": started.json()["customerKey"],
        },
        {
            "billingAuthId": started.json()["billingAuthId"],
            "authKey": 123,
            "customerKey": started.json()["customerKey"],
        },
        {
            "billingAuthId": started.json()["billingAuthId"],
            "authKey": "auth_123",
            "customerKey": 123,
        },
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/billing/issue",
            headers={**auth_headers, "Idempotency-Key": "billing-issue-invalid"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_issue_billing_key_reuses_idempotent_response(
    client,
    auth_headers,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )
    headers = {**auth_headers, "Idempotency-Key": "billing-issue-key"}
    payload = {
        "billingAuthId": started.json()["billingAuthId"],
        "authKey": "auth_123",
        "customerKey": started.json()["customerKey"],
    }

    first = client.post("/billing/issue", headers=headers, json=payload)
    second = client.post("/billing/issue", headers=headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()


def test_issue_billing_key_reuses_same_billing_auth_with_new_key(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )
    payload = {
        "billingAuthId": started.json()["billingAuthId"],
        "authKey": "auth_123",
        "customerKey": started.json()["customerKey"],
    }

    first = client.post(
        "/billing/issue",
        headers={**auth_headers, "Idempotency-Key": "billing-issue-key-1"},
        json=payload,
    )
    second = client.post(
        "/billing/issue",
        headers={**auth_headers, "Idempotency-Key": "billing-issue-key-2"},
        json=payload,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 1


def test_issue_billing_key_provider_failure_returns_402(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )
    test_dependencies.payment_provider.issue_billing_key_error = ProviderError(
        "인증 시간이 만료되었습니다.",
        provider_code="INVALID_AUTH_KEY",
    )
    headers = {**auth_headers, "Idempotency-Key": "billing-issue-key"}
    payload = {
        "billingAuthId": started.json()["billingAuthId"],
        "authKey": "auth_123",
        "customerKey": started.json()["customerKey"],
    }

    first = client.post("/billing/issue", headers=headers, json=payload)
    second = client.post("/billing/issue", headers=headers, json=payload)

    expected_failure = {
        "code": "BILLING_KEY_ISSUE_FAILED",
        "providerCode": "INVALID_AUTH_KEY",
        "message": "인증 시간이 만료되었습니다.",
        "retryable": True,
    }
    assert first.status_code == 402
    assert first.json() == {
        "billingAuthId": started.json()["billingAuthId"],
        "status": "failed",
        "failure": expected_failure,
    }
    assert second.status_code == 402
    assert second.json() == first.json()
    stored_auth = test_dependencies.billing_auths.auths[started.json()["billingAuthId"]]
    assert stored_auth.status == "failed"
    assert stored_auth.failure == expected_failure
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 1
    assert test_dependencies.billing_auth_issue_uow_factory.enter_count == 1
    assert test_dependencies.billing_auth_issue_uow_factory.commit_count == 1


def test_issue_billing_key_marks_expired_auth_and_returns_409(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )
    billing_auth_id = started.json()["billingAuthId"]
    test_dependencies.billing_auths.auths[billing_auth_id].expires_at = datetime(
        2026,
        6,
        9,
        tzinfo=UTC,
    )

    response = client.post(
        "/billing/issue",
        headers={**auth_headers, "Idempotency-Key": "billing-issue-key"},
        json={
            "billingAuthId": billing_auth_id,
            "authKey": "auth_123",
            "customerKey": started.json()["customerKey"],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
    assert test_dependencies.billing_auths.auths[billing_auth_id].status == "expired"
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 0
    assert test_dependencies.billing_auth_issue_uow_factory.enter_count == 0


def test_issue_billing_key_returns_400_for_mismatched_customer_key(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )

    response = client.post(
        "/billing/issue",
        headers={**auth_headers, "Idempotency-Key": "billing-issue-key"},
        json={
            "billingAuthId": started.json()["billingAuthId"],
            "authKey": "auth_123",
            "customerKey": "pcus_key_other",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 0


def test_issue_billing_key_returns_400_for_unknown_billing_auth(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    response = client.post(
        "/billing/issue",
        headers={**auth_headers, "Idempotency-Key": "billing-issue-key"},
        json={
            "billingAuthId": "bauth_missing",
            "authKey": "auth_123",
            "customerKey": "pcus_key_missing",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 0


def test_issue_billing_key_idempotency_conflict_returns_409(
    client,
    auth_headers,
) -> None:
    started = client.post(
        "/billing/auth",
        headers=auth_headers,
        json={
            "successUrl": "https://example.com/success",
            "failUrl": "https://example.com/fail",
            "setAsDefault": True,
        },
    )
    headers = {**auth_headers, "Idempotency-Key": "billing-issue-key"}
    first = client.post(
        "/billing/issue",
        headers=headers,
        json={
            "billingAuthId": started.json()["billingAuthId"],
            "authKey": "auth_123",
            "customerKey": started.json()["customerKey"],
        },
    )
    second = client.post(
        "/billing/issue",
        headers=headers,
        json={
            "billingAuthId": started.json()["billingAuthId"],
            "authKey": "auth_other",
            "customerKey": started.json()["customerKey"],
        },
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"
