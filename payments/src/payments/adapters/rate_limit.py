from __future__ import annotations

from datetime import datetime, timedelta

from payments.application.ports.admin_auth import AdminAuthRateLimiter


class InMemoryAdminAuthRateLimiter(AdminAuthRateLimiter):
    def __init__(self) -> None:
        self._attempts: dict[str, list[datetime]] = {}

    async def count_attempts(
        self,
        key: str,
        *,
        since: datetime,
    ) -> int:
        attempts = [
            attempt for attempt in self._attempts.get(key, []) if attempt >= since
        ]
        self._attempts[key] = attempts
        return len(attempts)

    async def record_attempt(
        self,
        key: str,
        *,
        attempted_at: datetime,
        window: timedelta,
    ) -> None:
        since = attempted_at - window
        attempts = [
            attempt for attempt in self._attempts.get(key, []) if attempt >= since
        ]
        attempts.append(attempted_at)
        self._attempts[key] = attempts
