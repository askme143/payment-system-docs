from __future__ import annotations

from datetime import UTC, datetime

from payments.application.ports.admin_operations import (
    AdminPaymentListRecord,
    AdminSubscriptionListRecord,
)
from payments.application.ports.provider import PaymentLookupProviderResult
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription


def test_list_admin_payments_requires_admin_context(client, auth_headers) -> None:
    response = client.get("/admin/payments", headers=auth_headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_list_admin_operator_audits_returns_summary_only(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.payment_stores.operator_audits.operator_audits["oaudit_1"] = (
        OperatorAudit(
            id="oaudit_1",
            operator_id="admin_1",
            action="scheduler.run_manual",
            target_type="scheduler_run",
            target_id="srun_1",
            previous_state={"status": None},
            next_state={"status": "succeeded"},
            reason_code="manual_retry_after_cron_failure",
            reason_message="CronJob failed",
            result="succeeded",
            request_ip="203.0.113.10",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
        )
    )

    response = client.get(
        "/admin/operator-audits?action=scheduler.run_manual",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "auditId": "oaudit_1",
                "operatorId": "admin_1",
                "action": "scheduler.run_manual",
                "targetType": "scheduler_run",
                "targetId": "srun_1",
                "result": "succeeded",
                "reasonCode": "manual_retry_after_cron_failure",
                "createdAt": "2026-06-10T00:00:00Z",
            }
        ],
        "page": {"nextCursor": None, "hasMore": False},
    }


def test_get_admin_operator_audit_returns_detail(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.payment_stores.operator_audits.operator_audits["oaudit_1"] = (
        OperatorAudit(
            id="oaudit_1",
            operator_id="admin_1",
            action="scheduler.run_manual",
            target_type="scheduler_run",
            target_id="srun_1",
            previous_state={"status": None},
            next_state={"status": "succeeded"},
            reason_code="manual_retry_after_cron_failure",
            reason_message="CronJob failed",
            result="succeeded",
            request_ip="203.0.113.10",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
            idempotency_scope="admin-scheduler-run",
        )
    )

    response = client.get("/admin/operator-audits/oaudit_1", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["auditId"] == "oaudit_1"
    assert body["previousState"] == {"status": None}
    assert body["nextState"] == {"status": "succeeded"}
    assert body["idempotencyScope"] == "admin-scheduler-run"


def test_list_admin_payments_returns_items(client, admin_headers) -> None:
    response = client.get("/admin/payments", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["paymentId"] == "pay_123"
    assert body["items"][0]["cancelUrl"] == "/admin/payments/pay_123/cancel"
    assert body["page"] == {"nextCursor": None, "hasMore": False}


def test_list_admin_payments_records_access_audit(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    response = client.get(
        "/admin/payments?status=paid&userId=user_1&paymentKey=paykey_123",
        headers={**admin_headers, "X-Request-Id": "req_admin_payment_list"},
    )

    assert response.status_code == 200
    audit = test_dependencies.admin_operations.audit_records[0]
    assert audit["action"] == "payment.list"
    assert audit["admin_id"] == "admin_1"
    assert audit["request_id"] == "req_admin_payment_list"
    assert audit["request_ip"] is not None
    assert audit["query"] == {
        "limit": 50,
        "status": ["paid"],
        "userId": "user_1",
        "paymentKey": "paykey_123",
    }
    assert audit["result_count"] == 1
    assert audit["has_more"] is False


def test_list_admin_payments_allows_payment_cancel_permission(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_auth.admin_accounts["admin_1"].permissions = [
        "payment_cancel"
    ]

    response = client.get("/admin/payments", headers=admin_headers)

    assert response.status_code == 200


def test_list_admin_payments_rejects_missing_payment_permission(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_auth.admin_accounts["admin_1"].permissions = [
        "subscription_read"
    ]

    response = client.get("/admin/payments", headers=admin_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_list_admin_payments_filters_by_documented_query_params(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_operations.payment_records.append(
        AdminPaymentListRecord(
            payment_id="pay_old",
            checkout_id="chk_old",
            user_id="user_2",
            user_email="other@example.com",
            order_id="order_old",
            order_name="Old order",
            payment_key="paykey_old",
            status="paid",
            amount=1000,
            paid_amount=1000,
            cancelable_amount=1000,
            currency="KRW",
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            approved_at=datetime(2026, 5, 1, tzinfo=UTC),
            method_summary="card 9999",
        )
    )

    response = client.get(
        "/admin/payments"
        "?userId=user_1"
        "&orderId=order_123"
        "&paymentKey=paykey_123"
        "&from=2026-06-01T00:00:00Z"
        "&to=2026-06-30T23:59:59Z",
        headers=admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["paymentId"] for item in body["items"]] == ["pay_123"]


def test_list_admin_payments_accepts_multiple_status_values(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_operations.payment_records.append(
        AdminPaymentListRecord(
            payment_id="pay_failed",
            checkout_id="chk_failed",
            user_id="user_1",
            user_email="customer@example.com",
            order_id="order_failed",
            order_name="Failed order",
            payment_key=None,
            status="failed",
            amount=1000,
            paid_amount=0,
            cancelable_amount=0,
            currency="KRW",
            created_at=datetime(2026, 6, 7, tzinfo=UTC),
            approved_at=datetime(2026, 6, 7, tzinfo=UTC),
            method_summary=None,
        )
    )

    response = client.get(
        "/admin/payments?status=paid&status=failed",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert [item["paymentId"] for item in response.json()["items"]] == [
        "pay_123",
        "pay_failed",
    ]
    failed_item = response.json()["items"][1]
    assert failed_item["cancelableAmount"] == 0
    assert failed_item["cancelUrl"] is None


def test_list_admin_payments_rejects_invalid_filters(
    client,
    admin_headers,
) -> None:
    invalid_status = client.get(
        "/admin/payments?status=refunded",
        headers=admin_headers,
    )
    invalid_range = client.get(
        "/admin/payments?from=2026-07-01T00:00:00Z&to=2026-06-01T00:00:00Z",
        headers=admin_headers,
    )
    invalid_from = client.get(
        "/admin/payments?from=not-a-date",
        headers=admin_headers,
    )
    invalid_limit = client.get(
        "/admin/payments?limit=101",
        headers=admin_headers,
    )

    assert invalid_status.status_code == 400
    assert invalid_range.status_code == 400
    assert invalid_from.status_code == 400
    assert invalid_limit.status_code == 400


def test_list_admin_payments_uses_cursor_pagination(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_operations.payment_records.append(
        AdminPaymentListRecord(
            payment_id="pay_newer",
            checkout_id="chk_newer",
            user_id="user_1",
            user_email="customer@example.com",
            order_id="order_newer",
            order_name="Newer order",
            payment_key="paykey_newer",
            status="paid",
            amount=1000,
            paid_amount=1000,
            cancelable_amount=1000,
            currency="KRW",
            created_at=datetime(2026, 6, 9, tzinfo=UTC),
            approved_at=datetime(2026, 6, 9, tzinfo=UTC),
            method_summary="card 1111",
        )
    )

    first = client.get("/admin/payments?limit=1", headers=admin_headers)
    cursor = first.json()["page"]["nextCursor"]
    second = client.get(
        f"/admin/payments?limit=1&cursor={cursor}",
        headers=admin_headers,
    )

    assert first.status_code == 200
    assert first.json()["items"][0]["paymentId"] == "pay_newer"
    assert first.json()["page"]["hasMore"] is True
    assert cursor is not None
    assert second.status_code == 200
    assert second.json()["items"][0]["paymentId"] == "pay_123"


def test_list_admin_subscriptions_returns_items(client, admin_headers) -> None:
    response = client.get("/admin/subscriptions", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["subscriptionId"] == "sub_123"
    assert body["items"][0]["paymentFailure"]["retryAvailable"] is False
    assert body["items"][0]["adjustUrl"] == "/admin/subscriptions/sub_123/adjust"
    assert body["page"] == {"nextCursor": None, "hasMore": False}


def test_list_admin_subscriptions_records_access_audit(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    response = client.get(
        "/admin/subscriptions"
        "?status=past_due&productCode=analytics&paymentFailure=true",
        headers={**admin_headers, "X-Request-Id": "req_admin_subscription_list"},
    )

    assert response.status_code == 200
    audit = test_dependencies.admin_operations.audit_records[0]
    assert audit["action"] == "subscription.list"
    assert audit["admin_id"] == "admin_1"
    assert audit["request_id"] == "req_admin_subscription_list"
    assert audit["request_ip"] is not None
    assert audit["query"] == {
        "limit": 50,
        "status": ["past_due"],
        "productCode": "analytics",
        "paymentFailure": True,
    }
    assert audit["result_count"] == 1
    assert audit["has_more"] is False


def test_list_admin_subscriptions_allows_subscription_adjust_permission(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_auth.admin_accounts["admin_1"].permissions = [
        "subscription_adjust"
    ]

    response = client.get("/admin/subscriptions", headers=admin_headers)

    assert response.status_code == 200


def test_list_admin_subscriptions_rejects_missing_subscription_permission(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_auth.admin_accounts["admin_1"].permissions = [
        "payment_read"
    ]

    response = client.get("/admin/subscriptions", headers=admin_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_list_admin_subscriptions_filters_by_documented_query_params(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_operations.subscription_records.append(
        AdminSubscriptionListRecord(
            subscription_id="sub_active",
            user_id="user_2",
            user_email="other@example.com",
            product_code="analytics",
            product_name="Analytics",
            plan_id="plan_basic",
            plan_name="Basic monthly",
            status="active",
            current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 30, tzinfo=UTC),
            next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
            payment_failure={"hasFailure": False},
            default_billing_method_summary="card 9999",
        )
    )

    response = client.get(
        "/admin/subscriptions"
        "?productCode=analytics"
        "&paymentFailure=true"
        "&nextBillingFrom=2026-06-30T00:00:00Z"
        "&nextBillingTo=2026-07-31T23:59:59Z",
        headers=admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["subscriptionId"] for item in body["items"]] == ["sub_123"]


def test_list_admin_subscriptions_accepts_multiple_status_values(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_operations.subscription_records.append(
        AdminSubscriptionListRecord(
            subscription_id="sub_active",
            user_id="user_2",
            user_email="other@example.com",
            product_code="analytics",
            product_name="Analytics",
            plan_id="plan_basic",
            plan_name="Basic monthly",
            status="active",
            current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 30, tzinfo=UTC),
            next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
            payment_failure={"hasFailure": False},
            default_billing_method_summary="card 9999",
        )
    )

    response = client.get(
        "/admin/subscriptions?status=past_due&status=active",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert [item["subscriptionId"] for item in response.json()["items"]] == [
        "sub_123",
        "sub_active",
    ]
    active_item = response.json()["items"][1]
    assert active_item["adjustUrl"] == "/admin/subscriptions/sub_active/adjust"


def test_list_admin_subscriptions_rejects_pending_status_filter(
    client,
    admin_headers,
) -> None:
    response = client.get(
        "/admin/subscriptions?status=pending",
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_list_admin_subscriptions_disables_adjust_url_for_canceled(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_operations.subscription_records = [
        AdminSubscriptionListRecord(
            subscription_id="sub_canceled",
            user_id="user_1",
            user_email="customer@example.com",
            product_code="analytics",
            product_name="Analytics",
            plan_id="plan_basic",
            plan_name="Basic monthly",
            status="canceled",
            current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 30, tzinfo=UTC),
            next_billing_at=None,
            payment_failure=None,
            default_billing_method_summary="card 9999",
        )
    ]

    response = client.get(
        "/admin/subscriptions?status=canceled",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["adjustUrl"] is None


def test_list_admin_subscriptions_rejects_invalid_filters(
    client,
    admin_headers,
) -> None:
    invalid_status = client.get(
        "/admin/subscriptions?status=paused",
        headers=admin_headers,
    )
    invalid_range = client.get(
        "/admin/subscriptions"
        "?nextBillingFrom=2026-08-01T00:00:00Z"
        "&nextBillingTo=2026-07-01T00:00:00Z",
        headers=admin_headers,
    )
    invalid_next_billing = client.get(
        "/admin/subscriptions?nextBillingFrom=not-a-date",
        headers=admin_headers,
    )
    invalid_payment_failure = client.get(
        "/admin/subscriptions?paymentFailure=maybe",
        headers=admin_headers,
    )
    invalid_limit = client.get(
        "/admin/subscriptions?limit=101",
        headers=admin_headers,
    )

    assert invalid_status.status_code == 400
    assert invalid_range.status_code == 400
    assert invalid_next_billing.status_code == 400
    assert invalid_payment_failure.status_code == 400
    assert invalid_limit.status_code == 400


def test_list_admin_subscriptions_uses_cursor_pagination(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_operations.subscription_records.append(
        AdminSubscriptionListRecord(
            subscription_id="sub_early",
            user_id="user_1",
            user_email="customer@example.com",
            product_code="analytics",
            product_name="Analytics",
            plan_id="plan_basic",
            plan_name="Basic monthly",
            status="active",
            current_period_start_at=datetime(2026, 5, 1, tzinfo=UTC),
            current_period_end_at=datetime(2026, 5, 31, tzinfo=UTC),
            next_billing_at=datetime(2026, 6, 15, tzinfo=UTC),
            payment_failure={"hasFailure": False},
            default_billing_method_summary="card 1234",
        )
    )

    first = client.get("/admin/subscriptions?limit=1", headers=admin_headers)
    cursor = first.json()["page"]["nextCursor"]
    second = client.get(
        f"/admin/subscriptions?limit=1&cursor={cursor}",
        headers=admin_headers,
    )

    assert first.status_code == 200
    assert first.json()["items"][0]["subscriptionId"] == "sub_early"
    assert first.json()["page"]["hasMore"] is True
    assert cursor is not None
    assert second.status_code == 200
    assert second.json()["items"][0]["subscriptionId"] == "sub_123"


def test_admin_operation_rejects_missing_permission(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    test_dependencies.admin_auth.admin_accounts["admin_1"].permissions = [
        "payment_read"
    ]

    response = client.post(
        "/admin/products",
        headers=admin_headers,
        json={
            "productCode": "ANALYTICS",
            "productType": "subscription",
            "name": "Analytics",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_cancel_admin_payment_returns_audit_id(
    client,
    auth_headers,
    admin_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json={
            "items": [{"skuId": "sku_report_pack_100", "quantity": 2}],
            "successUrl": "https://example.com/payments/success",
            "failUrl": "https://example.com/payments/fail",
        },
    )
    client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_admin",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers={**admin_headers, "Idempotency-Key": "admin-cancel-key"},
        json={
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
            "notifyCustomer": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["paymentId"] == created.json()["paymentId"]
    assert body["status"] == "canceled"
    assert body["operatorAuditId"].startswith("audit_")
    assert body["cancelHistory"][0]["requestedBy"] == "admin"
    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        body["operatorAuditId"]
    ]
    assert audit.request_ip is not None


def test_cancel_admin_payment_requires_idempotency_key(
    client,
    auth_headers,
    admin_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json={
            "items": [{"skuId": "sku_report_pack_100", "quantity": 2}],
            "successUrl": "https://example.com/payments/success",
            "failUrl": "https://example.com/payments/fail",
        },
    )
    client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_admin",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers=admin_headers,
        json={
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
            "notifyCustomer": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_cancel_admin_payment_rejects_blank_reason_fields(
    client,
    auth_headers,
    admin_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json={
            "items": [{"skuId": "sku_report_pack_100", "quantity": 2}],
            "successUrl": "https://example.com/payments/success",
            "failUrl": "https://example.com/payments/fail",
        },
    )
    client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_admin",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers={**admin_headers, "Idempotency-Key": "admin-cancel-blank-reason"},
        json={
            "cancelReason": " ",
            "reasonMessage": "duplicate order",
            "notifyCustomer": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_cancel_admin_payment_rejects_invalid_contract_values_as_400(
    client,
    admin_headers,
) -> None:
    invalid_payloads = [
        {
            "cancelAmount": "1000",
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
        },
        {
            "cancelReason": 123,
            "reasonMessage": "duplicate order",
        },
        {
            "cancelReason": "duplicate_payment",
            "reasonMessage": ["duplicate order"],
        },
        {
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
            "notifyCustomer": "yes",
        },
    ]

    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            "/admin/payments/pay_missing/cancel",
            headers={**admin_headers, "Idempotency-Key": f"invalid-contract-{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_cancel_admin_payment_returns_404_for_missing_payment(
    client,
    admin_headers,
) -> None:
    response = client.post(
        "/admin/payments/pay_missing/cancel",
        headers={**admin_headers, "Idempotency-Key": "missing-payment"},
        json={
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_cancel_admin_payment_rejects_invalid_amount_as_400(
    client,
    auth_headers,
    admin_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json={
            "items": [{"skuId": "sku_report_pack_100", "quantity": 2}],
            "successUrl": "https://example.com/payments/success",
            "failUrl": "https://example.com/payments/fail",
        },
    )
    client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_admin",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    zero_amount = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers={**admin_headers, "Idempotency-Key": "admin-cancel-zero"},
        json={
            "cancelAmount": 0,
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
        },
    )
    over_amount = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers={**admin_headers, "Idempotency-Key": "admin-cancel-over"},
        json={
            "cancelAmount": created.json()["amount"] + 1,
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
        },
    )

    assert zero_amount.status_code == 400
    assert over_amount.status_code == 400


def test_cancel_admin_payment_reuses_idempotent_response(
    client,
    auth_headers,
    admin_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json={
            "items": [{"skuId": "sku_report_pack_100", "quantity": 2}],
            "successUrl": "https://example.com/payments/success",
            "failUrl": "https://example.com/payments/fail",
        },
    )
    client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_admin",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )
    headers = {**admin_headers, "Idempotency-Key": "admin-cancel-key"}
    payload = {
        "cancelReason": "duplicate_payment",
        "reasonMessage": "duplicate order",
        "notifyCustomer": True,
    }

    first = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers=headers,
        json=payload,
    )
    second = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert test_dependencies.payment_provider.cancel_payment_call_count == 1


def test_cancel_admin_payment_idempotency_conflict_returns_409(
    client,
    auth_headers,
    admin_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json={
            "items": [{"skuId": "sku_report_pack_100", "quantity": 2}],
            "successUrl": "https://example.com/payments/success",
            "failUrl": "https://example.com/payments/fail",
        },
    )
    client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_admin",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )
    headers = {**admin_headers, "Idempotency-Key": "admin-cancel-key"}
    first = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers=headers,
        json={
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
            "notifyCustomer": True,
        },
    )
    second = client.post(
        f"/admin/payments/{created.json()['paymentId']}/cancel",
        headers=headers,
        json={
            "cancelAmount": 1,
            "cancelReason": "duplicate_payment",
            "reasonMessage": "duplicate order",
            "notifyCustomer": True,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_adjust_admin_subscription_postpones_next_billing(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_route",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-key"},
        json={
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "2026-06-08 incident compensation",
            "notifyCustomer": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionId"] == subscription.id
    assert body["adjustmentType"] == "postpone_next_billing"
    assert body["previousState"]["nextBillingAt"] == "2026-07-01T00:00:00Z"
    assert body["currentState"]["nextBillingAt"] == "2026-07-08T00:00:00Z"
    assert body["operatorAuditId"].startswith("audit_")
    assert body["notifiedCustomer"] is True
    assert test_dependencies.admin_operations.audit_records[0]["request_ip"] is not None
    assert test_dependencies.admin_subscription_adjust_uow_factory.enter_count == 1
    assert test_dependencies.admin_subscription_adjust_uow_factory.commit_count == 1
    assert test_dependencies.admin_subscription_adjust_uow_factory.rollback_count == 0


def test_adjust_admin_subscription_requires_idempotency_key(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_missing_key",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers=admin_headers,
        json={
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "2026-06-08 incident compensation",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_adjust_admin_subscription_rejects_blank_reason_fields(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_blank_reason",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-blank-reason"},
        json={
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": " ",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_adjust_admin_subscription_rejects_invalid_request_values_as_400(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_invalid_request",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    payloads = [
        {
            "adjustmentType": "unknown_adjustment",
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "unknown adjustment",
        },
        {
            "adjustmentType": "postpone_next_billing",
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "missing postpone days",
        },
        {
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 0},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "zero postpone days",
        },
        {
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": "7"},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "string postpone days",
        },
        {
            "adjustmentType": "postpone_next_billing",
            "postponeBy": ["days"],
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "invalid postpone object",
        },
        {
            "adjustmentType": "provider_payment_sync",
            "reasonCode": "webhook_recovery",
            "reasonMessage": "missing payment evidence",
        },
        {
            "adjustmentType": "provider_payment_sync",
            "paymentKey": 123,
            "reasonCode": "webhook_recovery",
            "reasonMessage": "invalid payment key",
        },
        {
            "adjustmentType": "set_next_billing_date",
            "nextBillingAt": "2026-06-09T00:00:00Z",
            "reasonCode": "migration_fix",
            "reasonMessage": "past next billing date",
        },
        {
            "adjustmentType": "set_next_billing_date",
            "nextBillingAt": "not-a-date",
            "reasonCode": "migration_fix",
            "reasonMessage": "invalid next billing date",
        },
        {
            "adjustmentType": "set_next_billing_date",
            "nextBillingAt": "2026-08-01T00:00:00",
            "reasonCode": "migration_fix",
            "reasonMessage": "missing timezone",
        },
        {
            "adjustmentType": "status_override",
            "reasonCode": "cs_exception",
            "reasonMessage": "missing target status",
        },
        {
            "adjustmentType": "status_override",
            "targetStatus": "paused",
            "reasonCode": "cs_exception",
            "reasonMessage": "invalid target status",
        },
        {
            "adjustmentType": "status_override",
            "targetStatus": "pending",
            "reasonCode": "cs_exception",
            "reasonMessage": "undocumented target status",
        },
        {
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": 123,
            "reasonMessage": "invalid reason code",
        },
        {
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": ["invalid reason"],
        },
        {
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "invalid notify customer",
            "notifyCustomer": "yes",
        },
    ]

    for index, payload in enumerate(payloads):
        response = client.post(
            f"/admin/subscriptions/{subscription.id}/adjust",
            headers={**admin_headers, "Idempotency-Key": f"adjust-invalid-{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_adjust_admin_subscription_rejects_postpone_for_cancel_scheduled(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_cancel_scheduled",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="cancel_scheduled",
        cancel_at_period_end=True,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-cancel-scheduled"},
        json={
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "2026-06-08 incident compensation",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_adjust_admin_subscription_rejects_set_next_billing_date_for_cancel_scheduled(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_set_next_cancel_scheduled",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="cancel_scheduled",
        cancel_at_period_end=True,
        next_billing_at=None,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-set-next-cancel"},
        json={
            "adjustmentType": "set_next_billing_date",
            "nextBillingAt": "2026-08-01T00:00:00Z",
            "reasonCode": "migration_fix",
            "reasonMessage": (
                "Do not restore billing date on cancel scheduled subscription"
            ),
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
    saved = test_dependencies.admin_operations.subscriptions[subscription.id]
    assert saved.status == "cancel_scheduled"
    assert saved.next_billing_at is None


def test_adjust_admin_subscription_syncs_provider_payment(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_provider_sync",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_adjust_provider_sync",
        order_id="order_sync",
        amount=9900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    invoice = Invoice(
        id="inv_adjust_provider_sync",
        user_id=subscription.user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    test_dependencies.admin_operations.payments[payment.id] = payment
    test_dependencies.admin_operations.invoices[invoice.id] = invoice

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-provider-sync"},
        json={
            "adjustmentType": "provider_payment_sync",
            "paymentKey": "paykey_provider_done",
            "invoiceId": invoice.id,
            "reasonCode": "webhook_recovery",
            "reasonMessage": "provider DONE was not reflected internally",
            "notifyCustomer": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["adjustmentType"] == "provider_payment_sync"
    assert body["currentState"]["status"] == "active"
    assert body["currentState"]["paymentStatus"] == "paid"
    assert body["currentState"]["invoiceStatus"] == "paid"
    assert body["currentState"]["providerPaymentKey"] == "paykey_provider_done"
    assert test_dependencies.admin_operations.payments[payment.id].status == "paid"
    assert test_dependencies.admin_operations.invoices[invoice.id].status == "paid"
    assert (
        test_dependencies.admin_operations.subscriptions[subscription.id].status
        == "active"
    )
    assert test_dependencies.admin_subscription_adjust_uow_factory.enter_count == 1
    assert test_dependencies.admin_subscription_adjust_uow_factory.commit_count == 1
    assert test_dependencies.admin_subscription_adjust_uow_factory.rollback_count == 0


def test_adjust_admin_subscription_provider_mismatch_returns_502(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_provider_mismatch",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_adjust_provider_mismatch",
        order_id="order_sync",
        amount=9900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    invoice = Invoice(
        id="inv_adjust_provider_mismatch",
        user_id=subscription.user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    test_dependencies.admin_operations.payments[payment.id] = payment
    test_dependencies.admin_operations.invoices[invoice.id] = invoice
    test_dependencies.payment_provider.get_payment_result = PaymentLookupProviderResult(
        payment_key="paykey_provider_mismatch",
        order_id=payment.order_id,
        status="DONE",
        total_amount=payment.amount + 1000,
        approved_at=datetime(2026, 6, 10, 1, 30, tzinfo=UTC),
        receipt_url="https://dashboard.tosspayments.com/receipt/provider-sync",
        method="카드",
        method_detail={},
        response_summary={"provider": "tosspayments", "status": "DONE"},
    )

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-provider-mismatch"},
        json={
            "adjustmentType": "provider_payment_sync",
            "paymentKey": "paykey_provider_mismatch",
            "invoiceId": invoice.id,
            "reasonCode": "payment_sync_mismatch",
            "reasonMessage": "provider amount mismatch",
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert test_dependencies.admin_operations.payments[payment.id].status == "failed"
    assert test_dependencies.admin_operations.audit_records[0]["result"] == "failed"
    assert test_dependencies.admin_subscription_adjust_uow_factory.enter_count == 1
    assert test_dependencies.admin_subscription_adjust_uow_factory.commit_count == 1
    assert test_dependencies.admin_subscription_adjust_uow_factory.rollback_count == 0


def test_adjust_admin_subscription_clears_payment_failure(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_clear_failure",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_adjust_clear_failure",
        order_id="order_clear_failure",
        amount=9900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 11, tzinfo=UTC),
        failure={"code": "CARD_DECLINED", "message": "card declined"},
    )
    invoice = Invoice(
        id="inv_adjust_clear_failure",
        user_id=subscription.user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    test_dependencies.admin_operations.payments[payment.id] = payment
    test_dependencies.admin_operations.invoices[invoice.id] = invoice

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-clear-failure"},
        json={
            "adjustmentType": "clear_payment_failure",
            "invoiceId": invoice.id,
            "targetStatus": "active",
            "reasonCode": "retry_recovered",
            "reasonMessage": "Customer paid via manual recovery",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["adjustmentType"] == "clear_payment_failure"
    assert body["previousState"]["retryAt"] == "2026-06-11T00:00:00Z"
    assert body["previousState"]["paymentStatus"] == "failed"
    assert body["previousState"]["invoiceStatus"] == "issued"
    assert body["currentState"]["status"] == "active"
    assert body["currentState"]["paymentStatus"] == "paid"
    assert body["currentState"]["invoiceStatus"] == "paid"
    assert body["currentState"]["retryAt"] is None
    assert body["currentState"]["paymentFailureReason"] is None
    saved_payment = test_dependencies.admin_operations.payments[payment.id]
    saved_invoice = test_dependencies.admin_operations.invoices[invoice.id]
    assert saved_payment.status == "paid"
    assert saved_payment.retry_scheduled_at is None
    assert saved_payment.failure is None
    assert saved_invoice.status == "paid"


def test_adjust_admin_subscription_reuses_idempotent_response(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_replay",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    headers = {**admin_headers, "Idempotency-Key": "adjust-replay-key"}
    payload = {
        "adjustmentType": "postpone_next_billing",
        "postponeBy": {"days": 7},
        "reasonCode": "service_incident_compensation",
        "reasonMessage": "2026-06-08 incident compensation",
        "notifyCustomer": True,
    }

    first = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers=headers,
        json=payload,
    )
    second = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(test_dependencies.admin_operations.audit_records) == 1
    assert (
        test_dependencies.admin_operations.subscriptions[
            subscription.id
        ].next_billing_at
        == datetime(2026, 7, 8, tzinfo=UTC)
    )


def test_adjust_admin_subscription_idempotency_conflict_returns_409(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_conflict",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    headers = {**admin_headers, "Idempotency-Key": "adjust-conflict-key"}
    first = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers=headers,
        json={
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 7},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "2026-06-08 incident compensation",
        },
    )
    second = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers=headers,
        json={
            "adjustmentType": "postpone_next_billing",
            "postponeBy": {"days": 8},
            "reasonCode": "service_incident_compensation",
            "reasonMessage": "2026-06-08 incident compensation",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_adjust_admin_subscription_rejects_invalid_status_override_transition(
    client,
    admin_headers,
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_invalid_status_override",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="canceled",
        cancel_at_period_end=False,
        next_billing_at=None,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    response = client.post(
        f"/admin/subscriptions/{subscription.id}/adjust",
        headers={**admin_headers, "Idempotency-Key": "adjust-invalid-status"},
        json={
            "adjustmentType": "status_override",
            "targetStatus": "active",
            "reasonCode": "cs_exception",
            "reasonMessage": "Reopen canceled subscription",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
    assert (
        test_dependencies.admin_operations.subscriptions[subscription.id].status
        == "canceled"
    )
