from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import httpx
from motor.motor_asyncio import AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document
from payments.application.notifications import (
    EnqueueNotificationCommand,
    enqueue_notification,
)
from payments.application.ports.clock import Clock
from payments.application.ports.notifications import (
    NotificationOutboxRepository,
    NotificationRecipientResolver,
    NotificationTemplateRepository,
    ResolvedNotificationRecipient,
    TemplateArgCipher,
)
from payments.domain.entities.admin_auth import AdminAccount
from payments.domain.entities.notification import JsonValue


@dataclass(frozen=True, slots=True)
class AdminAuthOutboxEmailSender:
    link_base_url: str
    outbox_repository: NotificationOutboxRepository
    template_repository: NotificationTemplateRepository
    recipient_resolver: NotificationRecipientResolver
    template_arg_cipher: TemplateArgCipher
    clock: Clock

    async def send_login_link(
        self,
        *,
        admin_id: str,
        email: str,
        recipient_name: str | None,
        auth_token_id: str,
        login_token: str,
        expires_at: datetime,
        request_ip: str | None,
        user_agent: str | None,
    ) -> None:
        _ = (email, recipient_name)
        template_args: dict[str, JsonValue] = {
            "loginLink": _link(
                self.link_base_url,
                "/admin/auth/login/confirm",
                "loginToken",
                login_token,
            ),
            "expiresMinutes": _expires_minutes(self.clock.utc_now(), expires_at),
        }
        if request_ip is not None:
            template_args["requestIp"] = request_ip
        if user_agent is not None:
            template_args["userAgent"] = user_agent
        await enqueue_notification(
            command=EnqueueNotificationCommand(
                event_type="admin_auth.login_link",
                recipient_type="admin",
                recipient_admin_id=admin_id,
                template_args=template_args,
                idempotency_key=f"email:admin_auth.login_link:{auth_token_id}",
                expires_at=expires_at,
            ),
            outbox_repository=self.outbox_repository,
            template_repository=self.template_repository,
            recipient_resolver=self.recipient_resolver,
            template_arg_cipher=self.template_arg_cipher,
            clock=self.clock,
        )

    async def send_password_reset_link(
        self,
        *,
        admin_id: str,
        email: str,
        recipient_name: str | None,
        auth_token_id: str,
        reset_token: str,
        expires_at: datetime,
        request_ip: str | None,
    ) -> None:
        _ = (email, recipient_name)
        template_args: dict[str, JsonValue] = {
            "resetLink": _link(
                self.link_base_url,
                "/admin/auth/password-reset/confirm",
                "resetToken",
                reset_token,
            ),
            "expiresMinutes": _expires_minutes(self.clock.utc_now(), expires_at),
        }
        if request_ip is not None:
            template_args["requestIp"] = request_ip
        await enqueue_notification(
            command=EnqueueNotificationCommand(
                event_type="admin_auth.password_reset",
                recipient_type="admin",
                recipient_admin_id=admin_id,
                template_args=template_args,
                idempotency_key=f"email:admin_auth.password_reset:{auth_token_id}",
                expires_at=expires_at,
            ),
            outbox_repository=self.outbox_repository,
            template_repository=self.template_repository,
            recipient_resolver=self.recipient_resolver,
            template_arg_cipher=self.template_arg_cipher,
            clock=self.clock,
        )


class HttpNotificationRecipientResolver:
    def __init__(
        self,
        *,
        recipient_api_base_url: str,
        admin_accounts: AsyncIOMotorCollection,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._recipient_api_base_url = recipient_api_base_url.rstrip("/")
        self._admin_accounts = admin_accounts
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def resolve_user(self, user_id: str) -> ResolvedNotificationRecipient:
        async with httpx.AsyncClient(
            base_url=self._recipient_api_base_url,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.get(f"/notification-recipients/users/{user_id}")
        if response.status_code == 404:
            raise ValueError("notification recipient was not found")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("notification recipient response must be an object")
        email = body.get("email")
        recipient_name = body.get("recipientName")
        if not isinstance(email, str) or not email:
            raise ValueError("notification recipient email is required")
        if recipient_name is not None and not isinstance(recipient_name, str):
            raise ValueError("notification recipient name must be a string")
        return ResolvedNotificationRecipient(
            recipient_type="user",
            recipient_user_id=user_id,
            recipient_admin_id=None,
            email=email,
            recipient_name=recipient_name,
        )

    async def resolve_admin(self, admin_id: str) -> ResolvedNotificationRecipient:
        admin = from_document(
            AdminAccount,
            await self._admin_accounts.find_one({"_id": admin_id}),
        )
        if admin is None:
            raise ValueError("notification admin recipient was not found")
        return ResolvedNotificationRecipient(
            recipient_type="admin",
            recipient_user_id=None,
            recipient_admin_id=admin.id,
            email=admin.email,
            recipient_name=admin.display_name,
        )


def _link(base_url: str, path: str, query_param: str, token: str) -> str:
    return f"{base_url.rstrip('/')}{path}?{urlencode({query_param: token})}"


def _expires_minutes(now: datetime, expires_at: datetime) -> int:
    seconds = max(0.0, (expires_at - now).total_seconds())
    return math.ceil(seconds / 60)
