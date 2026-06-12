from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime
from typing import Literal

from payments.domain.entities.subscription_change_preview import (
    SubscriptionChangePreview,
)

JsonObject = dict[str, object]


class HmacSubscriptionChangeTokenCodec:
    _PREFIX = "pct_"
    _VERSION = 1

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("subscription change token secret is required")
        self._secret = secret.encode()

    def encode_plan_change_preview(
        self,
        preview: SubscriptionChangePreview,
    ) -> str:
        payload = _preview_to_payload(preview)
        raw_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._secret, raw_payload, hashlib.sha256).digest()
        return (
            f"{self._PREFIX}{_base64_url_encode(raw_payload)}."
            f"{_base64_url_encode(signature)}"
        )

    def decode_plan_change_preview(
        self,
        confirmation_token: str,
    ) -> SubscriptionChangePreview | None:
        if not confirmation_token.startswith(self._PREFIX):
            return None
        encoded = confirmation_token.removeprefix(self._PREFIX)
        try:
            encoded_payload, encoded_signature = encoded.split(".", maxsplit=1)
            raw_payload = _base64_url_decode(encoded_payload)
            signature = _base64_url_decode(encoded_signature)
        except (ValueError, binascii.Error):
            return None
        expected_signature = hmac.new(
            self._secret,
            raw_payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            return None
        try:
            payload = json.loads(raw_payload.decode())
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return _preview_from_payload(confirmation_token, payload)


def _base64_url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _base64_url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}")


def _preview_to_payload(preview: SubscriptionChangePreview) -> JsonObject:
    return {
        "v": HmacSubscriptionChangeTokenCodec._VERSION,
        "sid": preview.subscription_id,
        "uid": preview.user_id,
        "product": preview.product_code,
        "current": preview.current_plan_id,
        "target": preview.target_plan_id,
        "decision": preview.server_decision,
        "willApply": preview.will_apply,
        "amount": preview.amount,
        "currency": preview.currency,
        "nextBillingDate": _datetime_to_payload(preview.next_billing_date),
        "expiresAt": _datetime_to_payload(preview.expires_at),
        "createdAt": _datetime_to_payload(preview.created_at),
    }


def _preview_from_payload(
    confirmation_token: str,
    payload: dict[object, object],
) -> SubscriptionChangePreview | None:
    if payload.get("v") != HmacSubscriptionChangeTokenCodec._VERSION:
        return None
    decision = _decision(payload.get("decision"))
    will_apply = _will_apply(payload.get("willApply"))
    expires_at = _datetime_from_payload(payload.get("expiresAt"))
    created_at = _datetime_from_payload(payload.get("createdAt"))
    next_billing_date = _datetime_from_payload(payload.get("nextBillingDate"))
    if (
        decision is None
        or will_apply is None
        or expires_at is None
        or created_at is None
    ):
        return None
    subscription_id = payload.get("sid")
    user_id = payload.get("uid")
    product_code = payload.get("product")
    current_plan_id = payload.get("current")
    target_plan_id = payload.get("target")
    amount = payload.get("amount")
    currency = payload.get("currency")
    if (
        not isinstance(subscription_id, str)
        or not isinstance(user_id, str)
        or not isinstance(product_code, str)
        or not isinstance(current_plan_id, str)
        or not isinstance(target_plan_id, str)
        or not isinstance(amount, int)
        or not isinstance(currency, str)
    ):
        return None
    return SubscriptionChangePreview(
        confirmation_token=confirmation_token,
        subscription_id=subscription_id,
        user_id=user_id,
        product_code=product_code,
        current_plan_id=current_plan_id,
        target_plan_id=target_plan_id,
        server_decision=decision,
        will_apply=will_apply,
        amount=amount,
        currency=currency,
        next_billing_date=next_billing_date,
        expires_at=expires_at,
        created_at=created_at,
    )


def _datetime_to_payload(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _datetime_from_payload(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _decision(value: object) -> Literal["upgrade", "downgrade"] | None:
    if value == "upgrade":
        return "upgrade"
    if value == "downgrade":
        return "downgrade"
    return None


def _will_apply(value: object) -> Literal["immediate", "next_billing_date"] | None:
    if value == "immediate":
        return "immediate"
    if value == "next_billing_date":
        return "next_billing_date"
    return None
