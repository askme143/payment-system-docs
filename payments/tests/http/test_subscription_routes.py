from __future__ import annotations

from datetime import UTC, datetime

from payments.application.ports import (
    DefaultBillingMethodSummary,
    SubscriptionAccountRecord,
)
from payments.application.ports.subscriptions import SubscriptionStatus
from payments.domain.entities.subscription import Subscription


def test_get_current_user_subscriptions_requires_user(client) -> None:
    response = client.get(
        "/subscriptions/me",
        headers={
            "Authorization": "Bearer secret",
            "X-Request-Id": "req_test",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_get_current_user_subscriptions_returns_user_rows(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.records["user_1"] = [
        SubscriptionAccountRecord(
            subscription_id="sub_old_basic",
            product_code="basic",
            plan_id="plan_basic_monthly",
            plan_name="Basic 월간",
            status="canceled",
            current_period_start_at=datetime(2026, 4, 10, tzinfo=UTC),
            current_period_end_at=datetime(2026, 5, 10, tzinfo=UTC),
            next_billing_at=None,
        ),
        SubscriptionAccountRecord(
            subscription_id="sub_active",
            product_code="basic",
            plan_id="plan_basic_monthly",
            plan_name="Basic 월간",
            status="active",
            current_period_start_at=datetime(2026, 5, 10, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 10, tzinfo=UTC),
            next_billing_at=datetime(2026, 6, 10, tzinfo=UTC),
        ),
        SubscriptionAccountRecord(
            subscription_id="sub_canceled",
            product_code="reports",
            plan_id="plan_reports_monthly",
            plan_name="Reports 월간",
            status="canceled",
            current_period_start_at=datetime(2026, 5, 10, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 10, tzinfo=UTC),
            next_billing_at=None,
        ),
    ]
    test_dependencies.subscription_accounts.billing_methods["user_1"] = (
        DefaultBillingMethodSummary(
            billing_method_id="bm_123",
            is_default=True,
            display_name="현대카드 **** 1234",
        )
    )

    response = client.get("/subscriptions/me", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["billingMethod"] == {
        "billingMethodId": "bm_123",
        "isDefault": True,
        "displayName": "현대카드 **** 1234",
    }
    assert [item["subscriptionId"] for item in body["subscriptions"]] == [
        "sub_active",
        "sub_canceled",
    ]
    assert body["subscriptions"][0]["resumeAvailable"] is False
    assert body["subscriptions"][0]["resubscribeUrl"] is None
    assert body["subscriptions"][0]["currentPeriodStart"] == "2026-05-10"
    assert body["subscriptions"][0]["currentPeriodEnd"] == "2026-06-10"
    assert body["subscriptions"][0]["nextBillingDate"] == "2026-06-10"
    assert body["subscriptions"][1]["resumeAvailable"] is False
    assert body["subscriptions"][1]["resubscribeUrl"] == (
        "/subscriptions/checkout?productCode=reports"
    )


def subscription_entity(
    *,
    subscription_id: str = "sub_123",
    user_id: str = "user_1",
    status: SubscriptionStatus = "active",
) -> Subscription:
    current_period_end_at = datetime(2026, 7, 8, tzinfo=UTC)
    return Subscription(
        id=subscription_id,
        user_id=user_id,
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status=status,
        cancel_at_period_end=status == "cancel_scheduled",
        next_billing_at=(
            current_period_end_at if status == "active" else None
        ),
        current_period_start_at=datetime(2026, 6, 8, tzinfo=UTC),
        current_period_end_at=current_period_end_at,
        cancel_at=(
            current_period_end_at if status == "cancel_scheduled" else None
        ),
        access_until=(
            current_period_end_at if status == "cancel_scheduled" else None
        ),
    )


def test_cancel_subscription_schedules_period_end_cancel(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity()
    )

    response = client.post(
        "/subscriptions/sub_123/cancel",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "subscriptionId": "sub_123",
        "status": "cancel_scheduled",
        "cancelAt": "2026-07-08",
        "currentPeriodEnd": "2026-07-08",
        "nextBillingDate": None,
        "accessUntil": "2026-07-08",
        "resumeAvailable": True,
    }


def test_cancel_subscription_reuses_idempotent_response(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity()
    )
    headers = {**auth_headers, "Idempotency-Key": "cancel-key"}
    payload = {"cancelReason": "too_expensive", "feedback": "not using"}

    first = client.post(
        "/subscriptions/sub_123/cancel",
        headers=headers,
        json=payload,
    )
    second = client.post(
        "/subscriptions/sub_123/cancel",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


def test_cancel_subscription_redacts_sensitive_feedback_in_audit(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity()
    )

    response = client.post(
        "/subscriptions/sub_123/cancel",
        headers=auth_headers,
        json={
            "cancelReason": "too_expensive",
            "feedback": (
                "email user@example.com phone 010-1234-5678 "
                "account 1234567890123"
            ),
        },
    )

    audit = next(
        iter(test_dependencies.payment_stores.operator_audits.operator_audits.values())
    )
    assert response.status_code == 200
    assert audit.reason_message == (
        "email [redacted] phone [redacted] account [redacted]"
    )
    assert audit.next_state["feedback"] == (
        "email [redacted] phone [redacted] account [redacted]"
    )


def test_cancel_subscription_idempotency_conflict_returns_409(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity()
    )
    headers = {**auth_headers, "Idempotency-Key": "cancel-key"}

    first = client.post(
        "/subscriptions/sub_123/cancel",
        headers=headers,
        json={"cancelReason": "too_expensive"},
    )
    second = client.post(
        "/subscriptions/sub_123/cancel",
        headers=headers,
        json={"cancelReason": "missing_feature"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_cancel_subscription_returns_403_for_other_user(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity(user_id="user_2")
    )

    response = client.post(
        "/subscriptions/sub_123/cancel",
        headers=auth_headers,
        json={},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_resume_subscription_returns_403_for_other_user(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity(user_id="user_2", status="cancel_scheduled")
    )

    response = client.post(
        "/subscriptions/sub_123/resume",
        headers=auth_headers,
        json={},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_resume_subscription_restores_active_status(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity(status="cancel_scheduled")
    )

    response = client.post(
        "/subscriptions/sub_123/resume",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["subscriptionId"] == "sub_123"
    assert response.json()["status"] == "active"
    assert response.json()["cancelAt"] is None
    assert response.json()["nextBillingDate"] == "2026-07-08"
    assert response.json()["resumeAvailable"] is False
    assert "accessUntil" not in response.json()


def test_resume_subscription_reuses_idempotent_response(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = (
        subscription_entity(status="cancel_scheduled")
    )
    headers = {**auth_headers, "Idempotency-Key": "resume-key"}
    payload = {"resumeReason": "changed_mind"}

    first = client.post(
        "/subscriptions/sub_123/resume",
        headers=headers,
        json=payload,
    )
    second = client.post(
        "/subscriptions/sub_123/resume",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
