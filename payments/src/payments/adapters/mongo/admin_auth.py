from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document, to_document
from payments.domain.entities.admin_auth import AdminAccount, AdminAuthToken


class MongoAdminAuthRepository:
    def __init__(
        self,
        admin_accounts: AsyncIOMotorCollection,
        admin_auth_tokens: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._admin_accounts = admin_accounts
        self._admin_auth_tokens = admin_auth_tokens
        self._session = session

    async def get_admin_by_email_lower(
        self,
        email_lower: str,
    ) -> AdminAccount | None:
        return from_document(
            AdminAccount,
            await self._admin_accounts.find_one(
                {"email_lower": email_lower},
                session=self._session,
            ),
        )

    async def get_admin_account(self, admin_id: str) -> AdminAccount | None:
        return from_document(
            AdminAccount,
            await self._admin_accounts.find_one(
                {"_id": admin_id},
                session=self._session,
            ),
        )

    async def save_admin_account(self, admin: AdminAccount) -> None:
        await self._admin_accounts.replace_one(
            {"_id": admin.id},
            to_document(admin, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def save_auth_token(self, token: AdminAuthToken) -> None:
        await self._admin_auth_tokens.replace_one(
            {"_id": token.id},
            to_document(token, omit_none=True),
            upsert=True,
            session=self._session,
        )

    async def get_auth_token_by_hash(
        self,
        token_hash: str,
    ) -> AdminAuthToken | None:
        return from_document(
            AdminAuthToken,
            await self._admin_auth_tokens.find_one(
                {"token_hash": token_hash},
                session=self._session,
            ),
        )

    async def revoke_active_refresh_tokens(
        self,
        admin_account_id: str,
        revoked_at: datetime,
        request_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        update_fields: dict[str, object] = {
            "status": "revoked",
            "consumed_at": revoked_at,
            "last_used_at": revoked_at,
        }
        if request_ip is not None:
            update_fields["request_ip"] = request_ip
        if user_agent is not None:
            update_fields["user_agent"] = user_agent
        await self._admin_auth_tokens.update_many(
            {
                "admin_account_id": admin_account_id,
                "token_type": "refresh_token",
                "status": "active",
            },
            {"$set": update_fields},
            session=self._session,
        )


class RecordingAdminAuthEmailSender:
    def __init__(self) -> None:
        self.login_links: list[tuple[str, str]] = []
        self.password_reset_links: list[tuple[str, str]] = []

    async def send_login_link(self, email: str, login_token: str) -> None:
        self.login_links.append((email, login_token))

    async def send_password_reset_link(self, email: str, reset_token: str) -> None:
        self.password_reset_links.append((email, reset_token))
