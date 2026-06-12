from __future__ import annotations

from types import TracebackType

from payments.adapters import toss
from payments.adapters.toss import TossPaymentProvider


class _FakeTossResponse:
    def __init__(self, *, payload: dict[str, object] | None = None) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        if self._payload is not None:
            return self._payload
        return {
            "paymentKey": "pay_key",
            "orderId": "order_123",
            "totalAmount": 9900,
            "approvedAt": "2026-06-10T00:01:00+00:00",
            "receipt": {"url": "https://receipt.example.test"},
            "method": "카드",
            "card": {"company": "현대", "number": "**** **** **** 1234"},
            "status": "DONE",
        }


class _FakeAsyncClient:
    last_headers: dict[str, str] | None = None
    last_json: dict[str, object] | None = None

    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
    ) -> _FakeTossResponse:
        _FakeAsyncClient.last_headers = headers
        _FakeAsyncClient.last_json = json
        if url.endswith("/cancel"):
            return _FakeTossResponse(
                payload={
                    "canceledAmount": 9900,
                    "cancelableAmount": 0,
                    "receipt": {"url": "https://receipt.example.test/cancel"},
                    "cancels": [
                        {
                            "transactionKey": "cnl_123",
                            "cancelAmount": 9900,
                            "canceledAt": "2026-06-10T00:02:00+00:00",
                        }
                    ],
                }
            )
        return _FakeTossResponse()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> _FakeTossResponse:
        _FakeAsyncClient.last_headers = headers
        return _FakeTossResponse()


async def test_toss_billing_charge_sends_idempotency_key(monkeypatch) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")

    await provider.charge_billing_key(
        billing_key="billing_key",
        customer_key="customer_key",
        order_id="order_123",
        amount=9900,
        order_name="Subscription billing",
        idempotency_key="sub_1:2026-06-10T00:00:00+00:00",
    )

    assert _FakeAsyncClient.last_headers is not None
    assert (
        _FakeAsyncClient.last_headers["Idempotency-Key"]
        == "sub_1:2026-06-10T00:00:00+00:00"
    )


async def test_toss_payment_confirm_sends_idempotency_key(monkeypatch) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")

    await provider.confirm_payment(
        payment_key="pay_key",
        order_id="order_123",
        amount=9900,
        idempotency_key="confirm-key",
    )

    assert _FakeAsyncClient.last_headers is not None
    assert _FakeAsyncClient.last_headers["Idempotency-Key"] == "confirm-key"


async def test_toss_payment_confirm_maps_documented_payment_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")

    result = await provider.confirm_payment(
        payment_key="pay_key",
        order_id="order_123",
        amount=9900,
    )

    assert result.method_detail == {
        "type": "card",
        "company": "현대",
        "maskedCardNumber": "**** **** **** 1234",
    }
    assert result.response_summary == {
        "provider": "tosspayments",
        "providerStatus": "DONE",
        "paymentKey": "pay_key",
        "orderId": "order_123",
        "totalAmount": 9900,
        "approvedAt": result.approved_at,
        "method": "카드",
        "receiptUrl": "https://receipt.example.test",
    }
    assert "status" not in result.response_summary


async def test_toss_payment_lookup_maps_documented_payment_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")

    result = await provider.get_payment(payment_key="pay_key")

    assert result.method_detail == {
        "type": "card",
        "company": "현대",
        "maskedCardNumber": "**** **** **** 1234",
    }
    assert result.response_summary == {
        "provider": "tosspayments",
        "providerStatus": "DONE",
        "paymentKey": "pay_key",
        "orderId": "order_123",
        "totalAmount": 9900,
        "approvedAt": result.approved_at,
        "method": "카드",
        "receiptUrl": "https://receipt.example.test",
    }
    assert "status" not in result.response_summary


async def test_toss_billing_charge_maps_documented_payment_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")

    result = await provider.charge_billing_key(
        billing_key="billing_key",
        customer_key="customer_key",
        order_id="order_123",
        amount=9900,
        order_name="Subscription billing",
    )

    assert result.method_detail == {
        "type": "card",
        "company": "현대",
        "maskedCardNumber": "**** **** **** 1234",
    }
    assert result.response_summary == {
        "provider": "tosspayments",
        "providerStatus": "DONE",
        "paymentKey": "pay_key",
        "orderId": "order_123",
        "totalAmount": 9900,
        "approvedAt": result.approved_at,
        "method": "카드",
        "receiptUrl": "https://receipt.example.test",
    }
    assert "status" not in result.response_summary


async def test_toss_payment_cancel_sends_idempotency_key(monkeypatch) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")

    await provider.cancel_payment(
        payment_key="pay_key",
        cancel_amount=9900,
        cancel_reason="customer_request",
        idempotency_key="cancel-key",
    )

    assert _FakeAsyncClient.last_headers is not None
    assert _FakeAsyncClient.last_headers["Idempotency-Key"] == "cancel-key"


async def test_toss_payment_cancel_maps_receipt_url(monkeypatch) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")

    result = await provider.cancel_payment(
        payment_key="pay_key",
        cancel_amount=9900,
        cancel_reason="customer_request",
        idempotency_key="cancel-key",
    )

    assert result.receipt_url == "https://receipt.example.test/cancel"


async def test_toss_payment_cancel_sends_refund_receive_account(
    monkeypatch,
) -> None:
    monkeypatch.setattr(toss.httpx, "AsyncClient", _FakeAsyncClient)
    provider = TossPaymentProvider(secret_key="secret", base_url="https://api.test")
    refund_bank_account: dict[str, object] = {
        "bank": "088",
        "accountNumber": "1234567890",
        "holderName": "홍길동",
    }

    await provider.cancel_payment(
        payment_key="pay_key",
        cancel_amount=9900,
        cancel_reason="customer_request",
        refund_bank_account=refund_bank_account,
        idempotency_key="cancel-key",
    )

    assert _FakeAsyncClient.last_json is not None
    assert _FakeAsyncClient.last_json["refundReceiveAccount"] == refund_bank_account
