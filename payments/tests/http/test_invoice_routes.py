from __future__ import annotations

from datetime import UTC, date, datetime

from payments.application.ports import InvoiceDetailRecord, InvoiceListRecord
from payments.application.ports.invoices import (
    InvoiceStatus,
    PaymentStatus,
    SubscriptionStatus,
)


def invoice_list_record(
    invoice_id: str,
    billing_date: date,
    *,
    subscription_id: str | None = "sub_123",
    status: InvoiceStatus = "paid",
    payment_status: PaymentStatus | None = "paid",
    receipt_available: bool = True,
    failure_summary: str | None = None,
) -> InvoiceListRecord:
    return InvoiceListRecord(
        invoice_id=invoice_id,
        subscription_id=subscription_id,
        product_name="Analytics Pro",
        plan_name="월간 Pro",
        invoice_type="recurring",
        status=status,
        payment_status=payment_status,
        amount=9900,
        currency="KRW",
        billing_date=billing_date,
        paid_at=datetime(2026, 7, 8, 0, 1, 12, tzinfo=UTC),
        receipt_available=receipt_available,
        failure_summary=failure_summary,
    )


def invoice_detail_record(
    invoice_id: str,
    *,
    subscription_status: SubscriptionStatus | None = "canceled",
    status: InvoiceStatus = "issued",
    payment_status: PaymentStatus | None = "failed",
    paid_at: datetime | None = None,
    receipt_url: str | None = None,
    failure_code: str | None = "INSUFFICIENT_FUNDS",
    failure_reason: str | None = "provider_rejected",
    failure_message: str | None = "잔액 부족으로 결제가 실패했습니다.",
    failure_retryable: bool = True,
    retry_available: bool = True,
    retry_scheduled_at: datetime | None = datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
) -> InvoiceDetailRecord:
    return InvoiceDetailRecord(
        invoice_id=invoice_id,
        subscription_id="sub_123",
        subscription_status=subscription_status,
        status=status,
        payment_status=payment_status,
        amount=9900,
        currency="KRW",
        billing_date=date(2026, 7, 8),
        paid_at=paid_at,
        receipt_url=receipt_url,
        failure_code=failure_code,
        failure_reason=failure_reason,
        failure_message=failure_message,
        failure_retryable=failure_retryable,
        retry_available=retry_available,
        retry_scheduled_at=retry_scheduled_at,
    )


def test_list_invoices_requires_user(client) -> None:
    response = client.get(
        "/invoices",
        headers={
            "Authorization": "Bearer secret",
            "X-Request-Id": "req_test",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_list_invoices_returns_user_invoice_summaries(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.records["user_1"] = [
        invoice_list_record("inv_202607_123", date(2026, 7, 8))
    ]

    response = client.get("/invoices?limit=20", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "invoiceId": "inv_202607_123",
                "subscriptionId": "sub_123",
                "productName": "Analytics Pro",
                "planName": "월간 Pro",
                "invoiceType": "recurring",
                "status": "paid",
                "amount": 9900,
                "currency": "KRW",
                "billingDate": "2026-07-08",
                "paidAt": "2026-07-08T00:01:12Z",
                "receiptAvailable": True,
                "failureSummary": None,
                "detailUrl": "/invoices/inv_202607_123",
            }
        ],
        "page": {"limit": 20, "nextCursor": None},
    }


def test_list_invoices_filters_by_documented_query_params(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.records["user_1"] = [
        invoice_list_record("inv_202607_123", date(2026, 7, 8)),
        invoice_list_record(
            "inv_202606_999",
            date(2026, 6, 8),
            subscription_id="sub_999",
            status="issued",
            payment_status="failed",
        ),
    ]

    response = client.get(
        "/invoices"
        "?status=paid"
        "&subscriptionId=sub_123"
        "&from=2026-07-01"
        "&to=2026-07-31"
        "&limit=20",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["invoiceId"] for item in body["items"]] == ["inv_202607_123"]


def test_list_invoices_exposes_payment_status_only_for_failures(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.records["user_1"] = [
        invoice_list_record(
            "inv_paid",
            date(2026, 7, 8),
            status="paid",
            payment_status="paid",
            receipt_available=True,
        ),
        invoice_list_record(
            "inv_failed",
            date(2026, 6, 8),
            status="issued",
            payment_status="failed",
            receipt_available=True,
            failure_summary="잔액 부족",
        ),
    ]

    response = client.get("/invoices?limit=20", headers=auth_headers)

    assert response.status_code == 200
    paid_item, failed_item = response.json()["items"]
    assert "paymentStatus" not in paid_item
    assert paid_item["receiptAvailable"] is True
    assert paid_item["failureSummary"] is None
    assert failed_item["paymentStatus"] == "failed"
    assert failed_item["paidAt"] is None
    assert failed_item["receiptAvailable"] is False
    assert failed_item["failureSummary"] == "잔액 부족"


def test_list_invoices_filters_by_payment_status(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.records["user_1"] = [
        invoice_list_record("inv_paid", date(2026, 7, 8)),
        invoice_list_record(
            "inv_failed",
            date(2026, 6, 8),
            status="issued",
            payment_status="failed",
            receipt_available=False,
            failure_summary="잔액 부족",
        ),
    ]

    response = client.get(
        "/invoices?paymentStatus=failed&limit=20",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert [item["invoiceId"] for item in response.json()["items"]] == ["inv_failed"]
    assert response.json()["items"][0]["paymentStatus"] == "failed"


def test_list_invoices_rejects_invalid_cursor(client, auth_headers) -> None:
    response = client.get("/invoices?cursor=opaque", headers=auth_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_list_invoices_uses_cursor_pagination(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.records["user_1"] = [
        invoice_list_record("inv_202607_123", date(2026, 7, 8)),
        invoice_list_record("inv_202606_123", date(2026, 6, 8)),
    ]

    first = client.get("/invoices?limit=1", headers=auth_headers)
    cursor = first.json()["page"]["nextCursor"]
    second = client.get(f"/invoices?limit=1&cursor={cursor}", headers=auth_headers)

    assert first.status_code == 200
    assert first.json()["items"][0]["invoiceId"] == "inv_202607_123"
    assert cursor is not None
    assert second.status_code == 200
    assert second.json()["items"][0]["invoiceId"] == "inv_202606_123"
    assert second.json()["page"]["nextCursor"] is None


def test_list_invoices_rejects_invalid_status(client, auth_headers) -> None:
    response = client.get("/invoices?status=failed", headers=auth_headers)
    payment_status = client.get("/invoices?paymentStatus=paid", headers=auth_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert payment_status.status_code == 400
    assert payment_status.json()["error"]["code"] == "bad_request"


def test_list_invoices_rejects_invalid_limit_and_date_range(
    client,
    auth_headers,
) -> None:
    invalid_limit = client.get("/invoices?limit=51", headers=auth_headers)
    invalid_range = client.get(
        "/invoices?from=2026-07-31&to=2026-07-01",
        headers=auth_headers,
    )
    compact_date = client.get("/invoices?from=20260701", headers=auth_headers)
    week_date = client.get("/invoices?to=2026-W27-3", headers=auth_headers)

    assert invalid_limit.status_code == 400
    assert invalid_limit.json()["error"]["code"] == "bad_request"
    assert invalid_range.status_code == 400
    assert invalid_range.json()["error"]["code"] == "bad_request"
    assert compact_date.status_code == 400
    assert compact_date.json()["error"]["code"] == "bad_request"
    assert week_date.status_code == 400
    assert week_date.json()["error"]["code"] == "bad_request"


def test_get_invoice_detail_returns_failure_actions(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.details[("user_1", "inv_123")] = (
        invoice_detail_record(
            "inv_123",
            paid_at=datetime(2026, 7, 8, 0, 1, 12, tzinfo=UTC),
            receipt_url="https://pay.example.com/failed-receipt",
        )
    )

    response = client.get("/invoices/inv_123", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["invoiceId"] == "inv_123"
    assert body["subscriptionStatus"] == "canceled"
    assert body["paidAt"] is None
    assert body["receiptUrl"] is None
    assert body["failure"]["code"] == "INSUFFICIENT_FUNDS"
    assert body["retry"]["available"] is True
    assert body["actions"]["billingMethodUpdateUrl"].endswith("/billing-methods")


def test_get_invoice_detail_omits_billing_method_action_for_other_failures(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.details[("user_1", "inv_timeout")] = (
        invoice_detail_record(
            "inv_timeout",
            failure_code="PROVIDER_TIMEOUT",
            failure_reason="provider_error",
            failure_message="일시적인 결제 제공자 오류입니다.",
        )
    )

    response = client.get("/invoices/inv_timeout", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["failure"]["code"] == "PROVIDER_TIMEOUT"
    assert body["actions"]["billingMethodUpdateUrl"] is None


def test_get_invoice_detail_returns_paid_invoice_receipt_without_failure_action(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.details[("user_1", "inv_paid")] = (
        invoice_detail_record(
            "inv_paid",
            status="paid",
            payment_status="paid",
            paid_at=datetime(2026, 7, 8, 0, 1, 12, tzinfo=UTC),
            receipt_url="https://pay.example.com/receipt",
            failure_code=None,
            failure_reason=None,
            failure_message=None,
            failure_retryable=False,
            retry_available=False,
            retry_scheduled_at=None,
        )
    )

    response = client.get("/invoices/inv_paid", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "paid"
    assert body["paidAt"] == "2026-07-08T00:01:12Z"
    assert body["receiptUrl"] == "https://pay.example.com/receipt"
    assert body["failure"] is None
    assert body["retry"]["available"] is False
    assert body["actions"]["billingMethodUpdateUrl"] is None


def test_get_invoice_detail_returns_404_for_missing_invoice(
    client,
    auth_headers,
) -> None:
    response = client.get("/invoices/inv_missing", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_invoice_detail_returns_403_for_other_user(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    test_dependencies.invoices.owners["inv_123"] = "user_2"

    response = client.get("/invoices/inv_123", headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
