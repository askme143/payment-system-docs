from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from payments.domain.entities.admin_auth import AdminAccount, AdminAuthToken


class AdminAuthRepository(Protocol):
    async def get_admin_by_email_lower(
        self,
        email_lower: str,
    ) -> AdminAccount | None:
        raise NotImplementedError

    async def get_admin_account(self, admin_id: str) -> AdminAccount | None:
        raise NotImplementedError

    async def save_admin_account(self, admin: AdminAccount) -> None:
        raise NotImplementedError

    async def save_auth_token(self, token: AdminAuthToken) -> None:
        raise NotImplementedError

    async def get_auth_token_by_hash(
        self,
        token_hash: str,
    ) -> AdminAuthToken | None:
        raise NotImplementedError

    async def revoke_active_refresh_tokens(
        self,
        admin_account_id: str,
        revoked_at: datetime,
        request_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        raise NotImplementedError


class AdminAuthEmailSender(Protocol):
    async def send_login_link(self, email: str, login_token: str) -> None:
        raise NotImplementedError

    async def send_password_reset_link(self, email: str, reset_token: str) -> None:
        raise NotImplementedError


class AdminAuthRateLimiter(Protocol):
    async def count_attempts(
        self,
        key: str,
        *,
        since: datetime,
    ) -> int:
        raise NotImplementedError

    async def record_attempt(
        self,
        key: str,
        *,
        attempted_at: datetime,
        window: timedelta,
    ) -> None:
        raise NotImplementedError
