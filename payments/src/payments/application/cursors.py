from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import date, datetime

from payments.application.errors import BadRequestError


def encode_cursor(values: Mapping[str, object]) -> str:
    payload = {
        key: _json_value(value)
        for key, value in values.items()
        if value is not None
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, object]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadRequestError("cursor is invalid") from exc
    if not isinstance(payload, dict):
        raise BadRequestError("cursor is invalid")
    return dict(payload)


def _json_value(value: object) -> object:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
