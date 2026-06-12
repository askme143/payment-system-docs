from __future__ import annotations

from payments.application.errors import ProviderError
from payments.application.ports import (
    PaymentCancelProviderResult,
    PaymentConfirmProviderResult,
)


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

    assert response.status_code == 201
    body = response.json()
    assert body["checkoutId"].startswith("chk_")
    assert body["paymentId"].startswith("pay_")
    assert body["orderId"].startswith("order_")
    assert body["attemptNo"] == 1
    assert body["orderName"] == "REPORT_PACK_100"
    assert body["amount"] == 50000
    assert body["currency"] == "KRW"
    assert body["customerKey"].startswith("pcus_key_")
    assert body["clientKey"] == "test_ck_local"
    assert body["successUrl"].startswith("https://example.com/payments/success?")
    assert f"paymentId={body['paymentId']}" in body["successUrl"]
    assert body["failUrl"].startswith("https://example.com/payments/fail?")
    assert f"paymentId={body['paymentId']}" in body["failUrl"]
    assert body["status"] == "ready"
    assert body["expiresAt"] == "2026-06-10T00:30:00Z"


def test_create_payment_order_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    invalid_payloads = [
        {
            **order_payload(),
            "items": [{"productId": "prod_report_pack", "quantity": 1}],
        },
        {**order_payload(), "items": []},
        {**order_payload(), "items": [{"skuId": "sku_report_pack_100", "quantity": 0}]},
        {
            **order_payload(),
            "items": [{"skuId": "sku_report_pack_100", "quantity": "1"}],
        },
        {**order_payload(), "items": [{"skuId": "sku_report_pack_100"}]},
        {**order_payload(), "successUrl": "not-a-url"},
        {**order_payload(), "failUrl": 123},
        {**order_payload(), "checkoutId": 123},
    ]

    for payload in invalid_payloads:
        response = client.post("/payments/orders", headers=auth_headers, json=payload)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_create_payment_order_reuses_idempotent_response(client, auth_headers) -> None:
    headers = {**auth_headers, "Idempotency-Key": "same-key"}

    first = client.post("/payments/orders", headers=headers, json=order_payload())
    second = client.post("/payments/orders", headers=headers, json=order_payload())

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()


def test_idempotency_key_conflict_returns_409(client, auth_headers) -> None:
    headers = {**auth_headers, "Idempotency-Key": "same-key"}

    first = client.post("/payments/orders", headers=headers, json=order_payload())
    second = client.post("/payments/orders", headers=headers, json=order_payload(3))

    assert first.status_code == 201
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
    body = owned.json()
    assert body["paymentId"] == payment_id
    assert body["checkoutId"] == created.json()["checkoutId"]
    assert body["orderId"] == created.json()["orderId"]
    assert body["attemptNo"] == 1
    assert body["amount"] == created.json()["amount"]
    assert body["currency"] == "KRW"
    assert body["orderName"] == "REPORT_PACK_100"
    assert body["status"] == "ready"
    assert body["approvedAt"] is None
    assert body["receiptUrl"] is None
    assert body["method"] is None
    assert body["methodDetail"] is None
    assert body["failure"] is None
    assert body["retry"] == {"available": False}
    assert other_user.status_code == 403
    assert other_user.json()["error"]["code"] == "forbidden"


def test_get_payment_detail_lazy_expires_ready_payment(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    payment_id = created.json()["paymentId"]
    payment = test_dependencies.payment_stores.payments.payments[payment_id]
    payment.expires_at = test_dependencies.clock.utc_now()

    response = client.get(f"/payments/{payment_id}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["paymentId"] == payment_id
    assert body["status"] == "expired"
    assert body["failure"] == {
        "phase": "before_confirm",
        "reason": "auth_result_not_reported",
        "retryable": True,
    }
    assert body["retry"] == {
        "available": True,
        "action": "create_new_payment_attempt",
        "checkoutId": created.json()["checkoutId"],
    }
    assert sku.reserved_stock == 0


def test_record_payment_auth_result_returns_retry_instruction(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    payment_id = created.json()["paymentId"]

    response = client.post(
        f"/payments/{payment_id}/auth-result",
        headers=auth_headers,
        json={
            "orderId": created.json()["orderId"],
            "code": "PAY_PROCESS_CANCELED",
            "message": "user canceled",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["paymentId"] == payment_id
    assert body["checkoutId"] == created.json()["checkoutId"]
    assert body["status"] == "failed"
    assert body["failure"] == {
        "phase": "before_confirm",
        "reason": "user_canceled",
        "providerCode": "PAY_PROCESS_CANCELED",
        "message": "user canceled",
        "retryable": True,
    }
    assert body["retry"] == {
        "available": True,
        "action": "create_new_payment_attempt",
        "checkoutId": created.json()["checkoutId"],
    }


def test_record_payment_auth_result_preserves_expired_unreported_reason(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    payment_id = created.json()["paymentId"]
    payment = test_dependencies.payment_stores.payments.payments[payment_id]
    payment.expires_at = test_dependencies.clock.utc_now()

    response = client.post(
        f"/payments/{payment_id}/auth-result",
        headers=auth_headers,
        json={
            "orderId": created.json()["orderId"],
            "code": "PAY_PROCESS_CANCELED",
            "message": "user canceled after expiration",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "expired"
    assert body["failure"] == {
        "phase": "before_confirm",
        "reason": "auth_result_not_reported",
        "retryable": True,
    }
    assert body["retry"] == {
        "available": True,
        "action": "create_new_payment_attempt",
        "checkoutId": created.json()["checkoutId"],
    }
    assert sku.reserved_stock == 0


def test_record_payment_auth_result_rejects_order_mismatch_as_400(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )

    response = client.post(
        f"/payments/{created.json()['paymentId']}/auth-result",
        headers=auth_headers,
        json={
            "orderId": "other_order",
            "code": "PAY_PROCESS_CANCELED",
            "message": "user canceled",
        },
    )

    assert response.status_code == 400


def test_record_payment_auth_result_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )

    invalid_payloads = [
        {},
        {"orderId": 123, "code": "PAY_PROCESS_CANCELED"},
        {"orderId": created.json()["orderId"], "code": 123},
        {
            "orderId": created.json()["orderId"],
            "code": "PAY_PROCESS_CANCELED",
            "message": 123,
        },
    ]

    for payload in invalid_payloads:
        response = client.post(
            f"/payments/{created.json()['paymentId']}/auth-result",
            headers=auth_headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_record_payment_auth_result_returns_403_for_other_user(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )

    response = client.post(
        f"/payments/{created.json()['paymentId']}/auth-result",
        headers={**auth_headers, "X-Request-User-Id": "user_2"},
        json={
            "orderId": created.json()["orderId"],
            "code": "PAY_PROCESS_CANCELED",
            "message": "user canceled",
        },
    )

    assert response.status_code == 403


def test_record_payment_auth_result_rejects_idempotency_conflict(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    first = client.post(
        f"/payments/{created.json()['paymentId']}/auth-result",
        headers={**auth_headers, "Idempotency-Key": "auth-result-key"},
        json={
            "orderId": created.json()["orderId"],
            "code": "PAY_PROCESS_CANCELED",
            "message": "user canceled",
        },
    )
    conflict = client.post(
        f"/payments/{created.json()['paymentId']}/auth-result",
        headers={**auth_headers, "Idempotency-Key": "auth-result-key"},
        json={
            "orderId": created.json()["orderId"],
            "code": "PROVIDER_AUTH_FAILED",
            "message": "provider rejected",
        },
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_confirm_payment_returns_paid_payment(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )

    response = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["checkoutId"] == created.json()["checkoutId"]
    assert body["paymentId"] == created.json()["paymentId"]
    assert body["orderId"] == created.json()["orderId"]
    assert body["paymentKey"] == "paykey_123"
    assert body["status"] == "paid"
    assert body["amount"] == created.json()["amount"]
    assert body["currency"] == "KRW"
    assert body["method"] == "카드"
    invoice = next(iter(test_dependencies.payment_stores.invoices.invoices.values()))
    assert invoice.user_id == "user_1"
    assert invoice.payment_id == created.json()["paymentId"]
    assert invoice.status == "paid"
    assert invoice.receipt_url == body["receiptUrl"]


def test_confirm_payment_reuses_payment_success_with_new_idempotency_key(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    payload = {
        "paymentId": created.json()["paymentId"],
        "paymentKey": "paykey_123",
        "orderId": created.json()["orderId"],
        "amount": created.json()["amount"],
    }

    first = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key-1"},
        json=payload,
    )
    second = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key-2"},
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert test_dependencies.payment_provider.confirm_payment_call_count == 1


def test_confirm_payment_returns_402_and_failed_body_on_provider_error(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    test_dependencies.payment_provider.confirm_payment_error = ProviderError(
        "card company rejected payment",
        provider_code="REJECT_CARD_COMPANY",
    )

    response = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    assert response.status_code == 402
    body = response.json()
    assert body["checkoutId"] == created.json()["checkoutId"]
    assert body["paymentId"] == created.json()["paymentId"]
    assert body["status"] == "failed"
    assert body["failure"] == {
        "code": "PAYMENT_CONFIRM_FAILED",
        "providerCode": "REJECT_CARD_COMPANY",
        "message": "card company rejected payment",
        "retryable": True,
        "phase": "confirm",
        "reason": "provider_rejected",
    }
    assert body["retry"] == {
        "available": True,
        "action": "create_new_payment_attempt",
        "checkoutId": created.json()["checkoutId"],
    }


def test_confirm_payment_amount_mismatch_returns_400_and_failed_state(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )

    response = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"] + 1,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    payment = test_dependencies.payment_stores.payments.payments[
        created.json()["paymentId"]
    ]
    assert payment.status == "failed"
    assert payment.failure["reason"] == "validation_failed"
    assert test_dependencies.payment_provider.confirm_payment_call_count == 0


def test_confirm_payment_provider_mismatch_returns_402_and_failed_state(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    test_dependencies.payment_provider.confirm_payment_result = (
        PaymentConfirmProviderResult(
            payment_key="paykey_123",
            order_id=created.json()["orderId"],
            amount=created.json()["amount"] + 1,
            approved_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/payment",
            method="카드",
            method_detail={"maskedCardNumber": "**** **** **** 1234"},
            response_summary={"provider": "tosspayments"},
        )
    )

    response = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    assert response.status_code == 402
    body = response.json()
    assert body["failure"]["message"] == "provider response does not match"
    payment = test_dependencies.payment_stores.payments.payments[
        created.json()["paymentId"]
    ]
    assert payment.status == "failed"
    assert payment.failure == body["failure"]


def test_get_payment_detail_returns_paid_method_summary(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.get(
        f"/payments/{confirmed.json()['paymentId']}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["paymentId"] == confirmed.json()["paymentId"]
    assert body["status"] == "paid"
    assert body["approvedAt"] == confirmed.json()["approvedAt"]
    assert body["receiptUrl"] == confirmed.json()["receiptUrl"]
    assert body["method"] == "카드"
    assert body["methodDetail"] == {"maskedNumber": "**** **** **** 1234"}
    assert body["failure"] is None
    assert body["retry"] == {"available": False}


def test_confirm_payment_requires_idempotency_key(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )

    response = client.post(
        "/payments/confirm",
        headers=auth_headers,
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_confirm_payment_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    invalid_payloads = [
        {},
        {
            "paymentId": 123,
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
        {
            "paymentId": created.json()["paymentId"],
            "paymentKey": 123,
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
        {
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": 123,
            "amount": created.json()["amount"],
        },
        {
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": "50000",
        },
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/payments/confirm",
            headers={**auth_headers, "Idempotency-Key": "confirm-invalid"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_cancel_payment_returns_cancel_history(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.post(
        f"/payments/{confirmed.json()['paymentId']}/cancel",
        headers={**auth_headers, "Idempotency-Key": "cancel-key"},
        json={
            "cancelReason": "customer_request",
            "reasonMessage": "refund requested",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["paymentId"] == confirmed.json()["paymentId"]
    assert body["paymentKey"] == "paykey_123"
    assert body["status"] == "canceled"
    assert body["paidAmount"] == created.json()["amount"]
    assert body["canceledAmount"] == created.json()["amount"]
    assert body["cancelableAmount"] == 0
    assert body["latestCancel"]["cancelId"].startswith("pcancel_")
    assert body["latestCancel"]["providerCancelId"] == "cnl_123"
    assert body["latestCancel"]["cancelReason"] == "customer_request"
    assert body["cancelHistory"][0]["cancelId"].startswith("pcancel_")
    assert body["cancelHistory"][0]["providerCancelId"] == "cnl_123"
    assert body["cancelHistory"][0]["status"] == "succeeded"


def test_cancel_payment_requires_idempotency_key(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.post(
        f"/payments/{confirmed.json()['paymentId']}/cancel",
        headers=auth_headers,
        json={
            "cancelReason": "customer_request",
            "reasonMessage": "refund requested",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_cancel_payment_rejects_invalid_amount_as_400(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    zero_amount = client.post(
        f"/payments/{confirmed.json()['paymentId']}/cancel",
        headers={**auth_headers, "Idempotency-Key": "cancel-key-zero"},
        json={
            "cancelAmount": 0,
            "cancelReason": "customer_request",
        },
    )
    over_amount = client.post(
        f"/payments/{confirmed.json()['paymentId']}/cancel",
        headers={**auth_headers, "Idempotency-Key": "cancel-key-over"},
        json={
            "cancelAmount": created.json()["amount"] + 1,
            "cancelReason": "customer_request",
        },
    )

    assert zero_amount.status_code == 400
    assert over_amount.status_code == 400


def test_cancel_payment_rejects_blank_reason_as_400(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.post(
        f"/payments/{confirmed.json()['paymentId']}/cancel",
        headers={**auth_headers, "Idempotency-Key": "cancel-key-blank"},
        json={
            "cancelReason": "   ",
        },
    )

    assert response.status_code == 400


def test_cancel_payment_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    invalid_payloads = [
        {},
        {"cancelReason": 123},
        {"cancelReason": "customer_request", "cancelAmount": "1"},
        {"cancelReason": "customer_request", "reasonMessage": 123},
        {"cancelReason": "customer_request", "refundBankAccount": "bank"},
    ]

    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            f"/payments/{confirmed.json()['paymentId']}/cancel",
            headers={**auth_headers, "Idempotency-Key": f"cancel-invalid-{index}"},
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_cancel_payment_returns_403_for_other_user(client, auth_headers) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )

    response = client.post(
        f"/payments/{confirmed.json()['paymentId']}/cancel",
        headers={
            **auth_headers,
            "X-Request-User-Id": "user_2",
            "Idempotency-Key": "cancel-key",
        },
        json={
            "cancelReason": "customer_request",
            "reasonMessage": "refund requested",
        },
    )

    assert response.status_code == 403


def test_cancel_payment_returns_502_for_provider_mismatch(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    created = client.post(
        "/payments/orders",
        headers=auth_headers,
        json=order_payload(),
    )
    confirmed = client.post(
        "/payments/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-key"},
        json={
            "paymentId": created.json()["paymentId"],
            "paymentKey": "paykey_123",
            "orderId": created.json()["orderId"],
            "amount": created.json()["amount"],
        },
    )
    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_bad",
            cancel_amount=created.json()["amount"] - 1,
            canceled_amount=created.json()["amount"] - 1,
            cancelable_amount=1,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url=None,
        )
    )

    response = client.post(
        f"/payments/{confirmed.json()['paymentId']}/cancel",
        headers={**auth_headers, "Idempotency-Key": "cancel-key"},
        json={
            "cancelReason": "customer_request",
            "reasonMessage": "refund requested",
        },
    )

    assert response.status_code == 502
