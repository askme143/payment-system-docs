from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from payments.domain.entities.ids import generate_uuid_id

AdminAccountStatus = Literal["active", "disabled", "locked"]
AdminAuthTokenStatus = Literal["active", "rotated", "revoked", "consumed", "expired"]
AdminAuthTokenType = Literal["login_link", "refresh_token", "password_reset"]


@dataclass()
class AdminAccount:
    id: str
    email: str
    email_lower: str
    password_hash: str
    display_name: str
    status: AdminAccountStatus
    roles: list[str]
    permissions: list[str]
    permission_version: int
    failed_login_count: int
    created_at: datetime
    updated_at: datetime
    locked_until_at: datetime | None = None
    last_login_at: datetime | None = None
    last_login_ip: str | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("admin")


@dataclass()
class AdminAuthToken:
    id: str
    admin_account_id: str
    token_type: AdminAuthTokenType
    token_hash: str
    status: AdminAuthTokenStatus
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None
    last_used_at: datetime | None = None
    request_ip: str | None = None
    user_agent: str | None = None

    @classmethod
    def generate_id(cls) -> str:
        return generate_uuid_id("aatok")
