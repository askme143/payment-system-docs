from __future__ import annotations

from datetime import UTC, datetime

from payments.application.ports import BillingMethodRecord


def test_list_billing_methods_requires_user(client) -> None:
    response = client.get(
        "/billing/methods",
        headers={
            "Authorization": "Bearer secret",
            "X-Request-Id": "req_test",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_list_billing_methods_returns_delete_policy(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        ),
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        ),
    ]
    test_dependencies.billing_methods.active_subscription_counts["user_1"] = 2

    response = client.get("/billing/methods", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["defaultBillingMethodId"] == "bm_123"
    assert body["activeSubscriptionCount"] == 2
    assert body["items"][0] == {
        "billingMethodId": "bm_123",
        "status": "active",
        "isDefault": True,
        "method": "카드",
        "cardCompany": "현대",
        "maskedCardNumber": "**** **** **** 1234",
        "billingKeyStatus": "active",
        "deletable": False,
        "deleteBlockReason": "default_method",
        "createdAt": "2026-06-08T10:15:00Z",
    }
    assert body["items"][1]["billingMethodId"] == "bm_456"
    assert body["items"][1]["deletable"] is True
    assert body["items"][1]["deleteBlockReason"] is None


def test_set_default_billing_method_changes_default(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        ),
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        ),
    ]

    response = client.patch(
        "/billing/methods/bm_456/default",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["billingMethodId"] == "bm_456"
    assert body["isDefault"] is True
    assert body["previousDefaultBillingMethodId"] == "bm_123"
    assert body["defaultChangedAt"] == "2026-06-10T00:00:00Z"
    assert body["appliesTo"] == "all_active_subscriptions"


def test_set_default_billing_method_reuses_idempotent_response(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        ),
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        ),
    ]
    headers = {**auth_headers, "Idempotency-Key": "default-key"}

    first = client.patch("/billing/methods/bm_456/default", headers=headers)
    second = client.patch("/billing/methods/bm_456/default", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["previousDefaultBillingMethodId"] == "bm_123"


def test_set_default_billing_method_idempotency_conflict_returns_409(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        ),
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        ),
    ]
    headers = {**auth_headers, "Idempotency-Key": "default-key"}

    first = client.patch("/billing/methods/bm_456/default", headers=headers)
    second = client.patch("/billing/methods/bm_123/default", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_set_default_billing_method_returns_403_for_other_user(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_2"] = [
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        )
    ]

    response = client.patch(
        "/billing/methods/bm_456/default",
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_delete_billing_method_deactivates_non_default(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        ),
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        ),
    ]

    response = client.delete("/billing/methods/bm_456", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "billingMethodId": "bm_456",
        "status": "inactive",
        "deletedAt": "2026-06-10T00:00:00Z",
        "remainingActiveMethodCount": 1,
        "defaultBillingMethodId": "bm_123",
    }


def test_delete_billing_method_returns_403_for_other_user(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_2"] = [
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        )
    ]

    response = client.delete("/billing/methods/bm_456", headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_delete_billing_method_reuses_idempotent_response(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        ),
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="active",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="active",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        ),
    ]
    headers = {**auth_headers, "Idempotency-Key": "delete-key"}

    first = client.delete("/billing/methods/bm_456", headers=headers)
    second = client.delete("/billing/methods/bm_456", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


def test_delete_billing_method_returns_200_for_already_inactive_method(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        ),
        BillingMethodRecord(
            billing_method_id="bm_456",
            status="inactive",
            is_default=False,
            method="카드",
            card_company="신한",
            masked_card_number="**** **** **** 5678",
            billing_key_status="revoked",
            created_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        ),
    ]

    response = client.delete("/billing/methods/bm_456", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "billingMethodId": "bm_456",
        "status": "inactive",
        "deletedAt": "2026-06-10T00:00:00Z",
        "remainingActiveMethodCount": 1,
        "defaultBillingMethodId": "bm_123",
    }


def test_delete_billing_method_rejects_default_method(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=True,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        )
    ]

    response = client.delete("/billing/methods/bm_123", headers=auth_headers)

    assert response.status_code == 409
    assert response.json() == {
        "billingMethodId": "bm_123",
        "status": "active",
        "blocked": True,
        "blockReason": "default_method",
        "message": "기본 결제수단은 먼저 다른 결제수단을 기본값으로 지정해야 합니다.",
    }


def test_delete_billing_method_rejects_last_method_for_active_subscriptions(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.billing_methods.records["user_1"] = [
        BillingMethodRecord(
            billing_method_id="bm_123",
            status="active",
            is_default=False,
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            billing_key_status="active",
            created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
        )
    ]
    test_dependencies.billing_methods.active_subscription_counts["user_1"] = 1

    response = client.delete("/billing/methods/bm_123", headers=auth_headers)

    assert response.status_code == 409
    assert response.json() == {
        "billingMethodId": "bm_123",
        "status": "active",
        "blocked": True,
        "blockReason": "last_method_for_active_subscriptions",
        "message": (
            "활성 구독이 1개 이상 있는 회원은 공통 결제수단이 최소 1개 남아야 합니다."
        ),
    }
