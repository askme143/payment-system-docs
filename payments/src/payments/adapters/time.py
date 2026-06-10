from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)
