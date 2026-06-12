from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

import httpx

from payments.application.errors import ProviderError
from payments.application.ports.provider import (
    BillingChargeProviderResult,
    BillingKeyIssueProviderResult,
    PaymentCancelProviderResult,
    PaymentConfirmProviderResult,
    PaymentLookupProviderResult,
)


class TossPaymentProvider:
    def __init__(
        self,
        *,
        secret_key: str,
        base_url: str,
    ) -> None:
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")

    async def confirm_payment(
        self,
        *,
        payment_key: str,
        order_id: str,
        amount: int,
        idempotency_key: str | None = None,
    ) -> PaymentConfirmProviderResult:
        auth = base64.b64encode(f"{self._secret_key}:".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                response = await client.post(
                    "/v1/payments/confirm",
                    headers=headers,
                    json={
                        "paymentKey": payment_key,
                        "orderId": order_id,
                        "amount": amount,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise _provider_error("toss payment confirm failed", exc) from exc
        approved_at = payload.get("approvedAt")
        receipt_url = _receipt_url(payload)
        return PaymentConfirmProviderResult(
            payment_key=str(payload.get("paymentKey", payment_key)),
            order_id=str(payload.get("orderId", order_id)),
            amount=int(payload.get("totalAmount", amount)),
            approved_at=(
                datetime.fromisoformat(approved_at)
                if isinstance(approved_at, str)
                else datetime.now().astimezone()
            ),
            receipt_url=receipt_url,
            method=str(payload.get("method", "")),
            method_detail=_payment_method_detail(payload),
            response_summary=_payment_response_summary(payload, receipt_url),
        )

    async def cancel_payment(
        self,
        *,
        payment_key: str,
        cancel_amount: int,
        cancel_reason: str,
        refund_bank_account: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentCancelProviderResult:
        auth = base64.b64encode(f"{self._secret_key}:".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        body: dict[str, object] = {
            "cancelReason": cancel_reason,
            "cancelAmount": cancel_amount,
        }
        if refund_bank_account is not None:
            body["refundReceiveAccount"] = refund_bank_account
        try:
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                response = await client.post(
                    f"/v1/payments/{payment_key}/cancel",
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise _provider_error("toss payment cancel failed", exc) from exc
        cancels = payload.get("cancels")
        latest = cancels[-1] if isinstance(cancels, list) and cancels else {}
        canceled_at = latest.get("canceledAt") if isinstance(latest, dict) else None
        receipt_url = _receipt_url(payload)
        return PaymentCancelProviderResult(
            cancel_id=(
                str(latest.get("transactionKey", "cancel"))
                if isinstance(latest, dict)
                else "cancel"
            ),
            cancel_amount=int(latest.get("cancelAmount", cancel_amount))
            if isinstance(latest, dict)
            else cancel_amount,
            canceled_amount=int(payload.get("canceledAmount", cancel_amount)),
            cancelable_amount=int(payload.get("cancelableAmount", 0)),
            canceled_at=(
                datetime.fromisoformat(canceled_at)
                if isinstance(canceled_at, str)
                else datetime.now().astimezone()
            ),
            receipt_url=receipt_url,
        )

    async def get_payment(
        self,
        *,
        payment_key: str,
    ) -> PaymentLookupProviderResult:
        auth = base64.b64encode(f"{self._secret_key}:".encode()).decode()
        try:
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                response = await client.get(
                    f"/v1/payments/{payment_key}",
                    headers={"Authorization": f"Basic {auth}"},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise _provider_error("toss payment lookup failed", exc) from exc
        approved_at = payload.get("approvedAt")
        receipt_url = _receipt_url(payload)
        return PaymentLookupProviderResult(
            payment_key=str(payload.get("paymentKey", payment_key)),
            order_id=str(payload.get("orderId", "")),
            status=str(payload.get("status", "")),
            total_amount=int(payload.get("totalAmount", 0)),
            approved_at=(
                datetime.fromisoformat(approved_at)
                if isinstance(approved_at, str)
                else None
            ),
            receipt_url=receipt_url,
            method=str(payload.get("method", "")),
            method_detail=_payment_method_detail(payload),
            response_summary=_payment_response_summary(payload, receipt_url),
            canceled_amount=int(payload.get("canceledAmount", 0)),
            cancelable_amount=(
                int(payload["cancelableAmount"])
                if isinstance(payload.get("cancelableAmount"), int)
                else None
            ),
        )

    async def issue_billing_key(
        self,
        *,
        auth_key: str,
        customer_key: str,
    ) -> BillingKeyIssueProviderResult:
        auth = base64.b64encode(f"{self._secret_key}:".encode()).decode()
        try:
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                response = await client.post(
                    "/v1/billing/authorizations/issue",
                    headers={"Authorization": f"Basic {auth}"},
                    json={
                        "authKey": auth_key,
                        "customerKey": customer_key,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise _provider_error("toss billing key issue failed", exc) from exc
        card = payload.get("card")
        card_detail = card if isinstance(card, dict) else {}
        return BillingKeyIssueProviderResult(
            billing_key=str(payload.get("billingKey", "")),
            method=str(payload.get("method", "카드")),
            card_company=str(card_detail.get("company", "")),
            masked_card_number=str(card_detail.get("number", "")),
            response_summary={
                "customerKey": payload.get("customerKey"),
                "method": payload.get("method"),
                "card": {
                    "company": card_detail.get("company"),
                    "number": card_detail.get("number"),
                },
            },
        )

    async def charge_billing_key(
        self,
        *,
        billing_key: str,
        customer_key: str,
        order_id: str,
        amount: int,
        order_name: str,
        idempotency_key: str | None = None,
    ) -> BillingChargeProviderResult:
        auth = base64.b64encode(f"{self._secret_key}:".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                response = await client.post(
                    f"/v1/billing/{billing_key}",
                    headers=headers,
                    json={
                        "customerKey": customer_key,
                        "amount": amount,
                        "orderId": order_id,
                        "orderName": order_name,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise _provider_error("toss billing charge failed", exc) from exc
        approved_at = payload.get("approvedAt")
        receipt_url = _receipt_url(payload)
        return BillingChargeProviderResult(
            payment_key=str(payload.get("paymentKey", "")),
            order_id=str(payload.get("orderId", order_id)),
            amount=int(payload.get("totalAmount", amount)),
            approved_at=(
                datetime.fromisoformat(approved_at)
                if isinstance(approved_at, str)
                else datetime.now().astimezone()
            ),
            receipt_url=receipt_url,
            method=str(payload.get("method", "")),
            method_detail=_payment_method_detail(payload),
            response_summary=_payment_response_summary(payload, receipt_url),
        )


def _payment_response_summary(
    payload: dict[str, Any],
    receipt_url: str | None,
) -> dict[str, object]:
    summary: dict[str, object] = {"provider": "tosspayments"}
    _copy_if_present(summary, "providerStatus", payload.get("status"))
    _copy_if_present(summary, "paymentKey", payload.get("paymentKey"))
    _copy_if_present(summary, "orderId", payload.get("orderId"))
    _copy_if_present(summary, "totalAmount", payload.get("totalAmount"))
    approved_at = _datetime(payload.get("approvedAt"))
    if approved_at is not None:
        summary["approvedAt"] = approved_at
    _copy_if_present(summary, "method", payload.get("method"))
    if receipt_url is not None:
        summary["receiptUrl"] = receipt_url
    return summary


def _payment_method_detail(payload: dict[str, Any]) -> dict[str, object]:
    card = payload.get("card")
    if not isinstance(card, dict):
        return {}
    detail: dict[str, object] = {"type": "card"}
    _copy_if_present(detail, "company", card.get("company"))
    _copy_if_present(detail, "maskedCardNumber", card.get("number"))
    return detail


def _receipt_url(payload: dict[str, Any]) -> str | None:
    receipt = payload.get("receipt")
    if isinstance(receipt, dict) and isinstance(receipt.get("url"), str):
        return receipt["url"]
    receipt_url = payload.get("receiptUrl")
    return receipt_url if isinstance(receipt_url, str) else None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value)


def _copy_if_present(
    target: dict[str, object],
    key: str,
    value: object,
) -> None:
    if value is not None:
        target[key] = value


def _provider_error(default_message: str, exc: httpx.HTTPError) -> ProviderError:
    if not isinstance(exc, httpx.HTTPStatusError):
        return ProviderError(default_message)
    try:
        payload = exc.response.json()
    except ValueError:
        return ProviderError(default_message)
    if not isinstance(payload, dict):
        return ProviderError(default_message)
    provider_code = payload.get("code")
    message = payload.get("message")
    return ProviderError(
        message if isinstance(message, str) else default_message,
        provider_code=provider_code if isinstance(provider_code, str) else None,
    )
