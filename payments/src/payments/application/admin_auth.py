from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import pbkdf2_hmac
from typing import Literal

from payments.application.errors import (
    AccountLockedError,
    AuthenticationError,
    BadRequestError,
    ForbiddenError,
    RateLimitError,
)
from payments.application.ports.admin_auth import (
    AdminAuthEmailSender,
    AdminAuthRateLimiter,
    AdminAuthRepository,
)
from payments.application.ports.clock import Clock
from payments.application.ports.unit_of_work import AdminAuthUnitOfWorkFactory
from payments.domain.entities.admin_auth import AdminAccount, AdminAuthToken

LOGIN_LINK_TTL_SECONDS = 600
ACCESS_TOKEN_TTL_SECONDS = 900
REFRESH_TOKEN_TTL_DAYS = 7
PASSWORD_RESET_TTL_SECONDS = 1800
MAX_FAILED_LOGIN_COUNT = 5
PASSWORD_HASH_ITERATIONS = 120_000
ADMIN_AUTH_RATE_LIMIT_MAX_ATTEMPTS = 3
ADMIN_AUTH_RATE_LIMIT_WINDOW = timedelta(minutes=10)
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
logger = logging.getLogger(__name__)


class _RefreshTokenReuseDetected(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    admin_id: str
    email: str
    display_name: str
    roles: list[str]
    permissions: list[str]
    status: str


@dataclass(frozen=True, slots=True)
class AdminLoginAccepted:
    accepted: bool = True
    delivery: str = "email_link"
    expires_in_seconds: int = LOGIN_LINK_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class AdminTokenPair:
    admin: AdminPrincipal
    admin_access_token: str
    expires_in_seconds: int
    admin_refresh_token: str
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AdminRefreshResult:
    admin_access_token: str
    expires_in_seconds: int
    admin_refresh_token: str
    refresh_expires_at: datetime


async def start_admin_login(
    *,
    email: str,
    password: str,
    repository: AdminAuthRepository,
    email_sender: AdminAuthEmailSender,
    rate_limiter: AdminAuthRateLimiter,
    clock: Clock,
    request_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> AdminLoginAccepted:
    admin: AdminAccount | None = None
    auth_token: AdminAuthToken | None = None
    try:
        email_lower = _normalize_email(email)
        if not password:
            raise BadRequestError("password is required")
        now = clock.utc_now()
        admin = await repository.get_admin_by_email_lower(email_lower)
        if admin is None:
            raise AuthenticationError("admin credentials are invalid")
        _ensure_login_allowed(admin, now)
        if not verify_admin_password(password, admin.password_hash):
            admin.failed_login_count += 1
            if admin.failed_login_count >= MAX_FAILED_LOGIN_COUNT:
                admin.status = "locked"
                admin.locked_until_at = now + timedelta(minutes=15)
            admin.updated_at = now
            await repository.save_admin_account(admin)
            raise AuthenticationError("admin credentials are invalid")

        await _enforce_admin_auth_rate_limit(
            rate_limiter,
            scope="login-link",
            email_lower=email_lower,
            request_ip=request_ip,
            now=now,
        )
        admin.failed_login_count = 0
        admin.locked_until_at = None
        admin.updated_at = now
        login_token = _new_token("alt")
        await repository.save_admin_account(admin)
        auth_token = AdminAuthToken(
            id=AdminAuthToken.generate_id(),
            admin_account_id=admin.id,
            token_type="login_link",
            token_hash=_hash_token(login_token),
            status="active",
            expires_at=now + timedelta(seconds=LOGIN_LINK_TTL_SECONDS),
            created_at=now,
            request_ip=request_ip,
            user_agent=user_agent,
        )
        await repository.save_auth_token(auth_token)
        await email_sender.send_login_link(admin.email, login_token)
    except Exception:
        if auth_token is not None:
            auth_token.status = "revoked"
            auth_token.consumed_at = auth_token.created_at
            await repository.save_auth_token(auth_token)
        _log_admin_auth_access_event(
            event="login_link_request",
            result="failed",
            admin_id=admin.id if admin is not None else None,
            request_id=request_id,
            request_ip=request_ip,
            user_agent=user_agent,
        )
        raise
    _log_admin_auth_access_event(
        event="login_link_request",
        result="succeeded",
        admin_id=admin.id,
        request_id=request_id,
        request_ip=request_ip,
        user_agent=user_agent,
    )
    return AdminLoginAccepted()


async def confirm_admin_login(
    *,
    login_token: str,
    repository: AdminAuthRepository,
    clock: Clock,
    access_token_secret: str,
    admin_auth_uow_factory: AdminAuthUnitOfWorkFactory | None = None,
    request_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> AdminTokenPair:
    if not login_token.startswith("alt_"):
        raise BadRequestError("loginToken is invalid")
    now = clock.utc_now()
    if admin_auth_uow_factory is not None:
        async with admin_auth_uow_factory() as uow:
            return await _confirm_admin_login_with_repository(
                login_token=login_token,
                repository=uow.admin_auth,
                now=now,
                access_token_secret=access_token_secret,
                request_ip=request_ip,
                user_agent=user_agent,
                request_id=request_id,
            )
    return await _confirm_admin_login_with_repository(
        login_token=login_token,
        repository=repository,
        now=now,
        access_token_secret=access_token_secret,
        request_ip=request_ip,
        user_agent=user_agent,
        request_id=request_id,
    )


async def _confirm_admin_login_with_repository(
    *,
    login_token: str,
    repository: AdminAuthRepository,
    now: datetime,
    access_token_secret: str,
    request_ip: str | None,
    user_agent: str | None,
    request_id: str | None,
) -> AdminTokenPair:
    token = await _active_token(repository, login_token, "login_link", now)
    admin = await _active_admin(repository, token.admin_account_id)
    token.status = "consumed"
    token.consumed_at = now
    token.last_used_at = now
    await repository.save_auth_token(token)
    refresh_token, refresh_expires_at = await _issue_refresh_token(
        repository,
        admin,
        now,
        request_ip,
        user_agent,
    )
    admin.last_login_at = now
    admin.last_login_ip = request_ip
    admin.failed_login_count = 0
    admin.updated_at = now
    await repository.save_admin_account(admin)
    _log_admin_auth_access_event(
        event="login_confirm",
        result="succeeded",
        admin_id=admin.id,
        request_id=request_id,
        request_ip=request_ip,
        user_agent=user_agent,
    )
    return AdminTokenPair(
        admin=_principal(admin),
        admin_access_token=_sign_access_token(admin, now, access_token_secret),
        expires_in_seconds=ACCESS_TOKEN_TTL_SECONDS,
        admin_refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
    )


async def get_current_admin(
    *,
    admin_access_token: str,
    repository: AdminAuthRepository,
    clock: Clock,
    access_token_secret: str,
) -> AdminPrincipal:
    payload = _verify_access_token(
        admin_access_token,
        clock.utc_now(),
        access_token_secret,
    )
    admin = await _active_admin(repository, str(payload["adminId"]))
    if admin.permission_version != _int_claim(payload["permissionVersion"]):
        raise AuthenticationError("admin access token is stale")
    return _principal(admin)


async def refresh_admin_token(
    *,
    admin_refresh_token: str,
    repository: AdminAuthRepository,
    clock: Clock,
    access_token_secret: str,
    admin_auth_uow_factory: AdminAuthUnitOfWorkFactory | None = None,
    request_ip: str | None = None,
    user_agent: str | None = None,
) -> AdminRefreshResult:
    if not admin_refresh_token.startswith("art_"):
        raise BadRequestError("adminRefreshToken is invalid")
    now = clock.utc_now()
    if admin_auth_uow_factory is not None:
        refresh_reuse_detected = False
        async with admin_auth_uow_factory() as uow:
            try:
                return await _refresh_admin_token_with_repository(
                    admin_refresh_token=admin_refresh_token,
                    repository=uow.admin_auth,
                    now=now,
                    access_token_secret=access_token_secret,
                    request_ip=request_ip,
                    user_agent=user_agent,
                )
            except _RefreshTokenReuseDetected:
                refresh_reuse_detected = True
        if refresh_reuse_detected:
            raise AuthenticationError("admin refresh token is invalid")
    try:
        return await _refresh_admin_token_with_repository(
            admin_refresh_token=admin_refresh_token,
            repository=repository,
            now=now,
            access_token_secret=access_token_secret,
            request_ip=request_ip,
            user_agent=user_agent,
        )
    except _RefreshTokenReuseDetected as exc:
        raise AuthenticationError("admin refresh token is invalid") from exc


async def _refresh_admin_token_with_repository(
    *,
    admin_refresh_token: str,
    repository: AdminAuthRepository,
    now: datetime,
    access_token_secret: str,
    request_ip: str | None,
    user_agent: str | None,
) -> AdminRefreshResult:
    token_hash = _hash_token(admin_refresh_token)
    token = await repository.get_auth_token_by_hash(token_hash)
    if token is None:
        raise AuthenticationError("admin refresh token is invalid")
    if token.status != "active":
        await repository.revoke_active_refresh_tokens(token.admin_account_id, now)
        raise _RefreshTokenReuseDetected
    if token.token_type != "refresh_token" or token.expires_at <= now:
        raise AuthenticationError("admin refresh token is invalid")
    admin = await _active_admin(repository, token.admin_account_id)
    token.status = "rotated"
    token.consumed_at = now
    token.last_used_at = now
    await repository.save_auth_token(token)
    refresh_token, refresh_expires_at = await _issue_refresh_token(
        repository,
        admin,
        now,
        request_ip,
        user_agent,
    )
    return AdminRefreshResult(
        admin_access_token=_sign_access_token(admin, now, access_token_secret),
        expires_in_seconds=ACCESS_TOKEN_TTL_SECONDS,
        admin_refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
    )


async def logout_admin(
    *,
    admin_access_token: str,
    admin_refresh_token: str | None,
    repository: AdminAuthRepository,
    clock: Clock,
    access_token_secret: str,
    request_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> None:
    payload = _verify_access_token(
        admin_access_token,
        clock.utc_now(),
        access_token_secret,
    )
    admin_id = str(payload["adminId"])
    now = clock.utc_now()
    if admin_refresh_token is None:
        await repository.revoke_active_refresh_tokens(
            admin_id,
            now,
            request_ip=request_ip,
            user_agent=user_agent,
        )
        _log_admin_auth_access_event(
            event="logout",
            result="succeeded",
            admin_id=admin_id,
            request_id=request_id,
            request_ip=request_ip,
            user_agent=user_agent,
        )
        return
    token = await repository.get_auth_token_by_hash(_hash_token(admin_refresh_token))
    if token is not None and token.admin_account_id == admin_id:
        token.status = "revoked"
        token.consumed_at = now
        token.last_used_at = now
        token.request_ip = request_ip
        token.user_agent = user_agent
        await repository.save_auth_token(token)
    _log_admin_auth_access_event(
        event="logout",
        result="succeeded",
        admin_id=admin_id,
        request_id=request_id,
        request_ip=request_ip,
        user_agent=user_agent,
    )


async def request_admin_password_reset(
    *,
    email: str,
    repository: AdminAuthRepository,
    email_sender: AdminAuthEmailSender,
    rate_limiter: AdminAuthRateLimiter,
    clock: Clock,
    request_ip: str | None = None,
    user_agent: str | None = None,
) -> AdminLoginAccepted:
    email_lower = _normalize_email(email)
    now = clock.utc_now()
    await _enforce_admin_auth_rate_limit(
        rate_limiter,
        scope="password-reset",
        email_lower=email_lower,
        request_ip=request_ip,
        now=now,
    )
    admin = await repository.get_admin_by_email_lower(email_lower)
    if admin is not None and admin.status == "active":
        reset_token = _new_token("apr")
        await repository.save_auth_token(
            AdminAuthToken(
                id=AdminAuthToken.generate_id(),
                admin_account_id=admin.id,
                token_type="password_reset",
                token_hash=_hash_token(reset_token),
                status="active",
                expires_at=now + timedelta(seconds=PASSWORD_RESET_TTL_SECONDS),
                created_at=now,
                request_ip=request_ip,
                user_agent=user_agent,
            )
        )
        await email_sender.send_password_reset_link(admin.email, reset_token)
    return AdminLoginAccepted(delivery="password_reset", expires_in_seconds=0)


async def confirm_admin_password_reset(
    *,
    reset_token: str,
    new_password: str,
    repository: AdminAuthRepository,
    clock: Clock,
    admin_auth_uow_factory: AdminAuthUnitOfWorkFactory | None = None,
    request_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> None:
    if not reset_token.startswith("apr_"):
        raise BadRequestError("resetToken is invalid")
    _validate_new_password(new_password)
    now = clock.utc_now()
    if admin_auth_uow_factory is not None:
        async with admin_auth_uow_factory() as uow:
            await _confirm_admin_password_reset_with_repository(
                reset_token=reset_token,
                new_password=new_password,
                repository=uow.admin_auth,
                now=now,
                request_ip=request_ip,
                user_agent=user_agent,
                request_id=request_id,
            )
            return
    await _confirm_admin_password_reset_with_repository(
        reset_token=reset_token,
        new_password=new_password,
        repository=repository,
        now=now,
        request_ip=request_ip,
        user_agent=user_agent,
        request_id=request_id,
    )


async def _confirm_admin_password_reset_with_repository(
    *,
    reset_token: str,
    new_password: str,
    repository: AdminAuthRepository,
    now: datetime,
    request_ip: str | None,
    user_agent: str | None,
    request_id: str | None,
) -> None:
    token = await _active_token(repository, reset_token, "password_reset", now)
    admin = await _active_admin(repository, token.admin_account_id)
    if verify_admin_password(new_password, admin.password_hash):
        raise BadRequestError("newPassword cannot reuse current password")
    admin.password_hash = hash_admin_password(new_password)
    admin.failed_login_count = 0
    admin.locked_until_at = None
    admin.updated_at = now
    token.status = "consumed"
    token.consumed_at = now
    token.last_used_at = now
    token.request_ip = request_ip
    token.user_agent = user_agent
    await repository.save_admin_account(admin)
    await repository.save_auth_token(token)
    await repository.revoke_active_refresh_tokens(admin.id, now)
    _log_admin_auth_access_event(
        event="password_reset_confirm",
        result="succeeded",
        admin_id=admin.id,
        request_id=request_id,
        request_ip=request_ip,
        user_agent=user_agent,
    )


def hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return "pbkdf2_sha256${iterations}${salt}${digest}".format(
        iterations=PASSWORD_HASH_ITERATIONS,
        salt=base64.urlsafe_b64encode(salt).decode("ascii"),
        digest=base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_admin_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    actual = pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        base64.urlsafe_b64decode(salt.encode("ascii")),
        int(iterations),
    )
    return hmac.compare_digest(
        base64.urlsafe_b64encode(actual).decode("ascii"),
        expected,
    )


def _validate_new_password(password: str) -> None:
    if len(password) < 12:
        raise BadRequestError("newPassword is too short")
    if not any(character.isalpha() for character in password):
        raise BadRequestError("newPassword must include letters")
    if not any(not character.isalpha() for character in password):
        raise BadRequestError("newPassword must include a separator or number")


def _normalize_email(email: str) -> str:
    email_lower = email.strip().lower()
    if not _EMAIL_PATTERN.fullmatch(email_lower):
        raise BadRequestError("email is invalid")
    return email_lower


async def _enforce_admin_auth_rate_limit(
    rate_limiter: AdminAuthRateLimiter,
    *,
    scope: str,
    email_lower: str,
    request_ip: str | None,
    now: datetime,
) -> None:
    keys = _admin_auth_rate_limit_keys(
        scope=scope,
        email_lower=email_lower,
        request_ip=request_ip,
    )
    since = now - ADMIN_AUTH_RATE_LIMIT_WINDOW
    for key in keys:
        if (
            await rate_limiter.count_attempts(key, since=since)
            >= ADMIN_AUTH_RATE_LIMIT_MAX_ATTEMPTS
        ):
            raise RateLimitError("too many admin authentication requests")
    for key in keys:
        await rate_limiter.record_attempt(
            key,
            attempted_at=now,
            window=ADMIN_AUTH_RATE_LIMIT_WINDOW,
        )


def _admin_auth_rate_limit_keys(
    *,
    scope: str,
    email_lower: str,
    request_ip: str | None,
) -> list[str]:
    keys = [f"{scope}:email:{_hash_rate_limit_value(email_lower)}"]
    if request_ip is not None:
        keys.append(f"{scope}:ip:{request_ip}")
    return keys


def _hash_rate_limit_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _log_admin_auth_access_event(
    *,
    event: str,
    result: Literal["succeeded", "failed"],
    admin_id: str | None,
    request_id: str | None,
    request_ip: str | None,
    user_agent: str | None,
) -> None:
    logger.info(
        "admin_auth_access",
        extra={
            "payment_auth_event": event,
            "payment_auth_result": result,
            "payment_admin_id": admin_id,
            "payment_request_id": request_id,
            "payment_request_ip": request_ip,
            "payment_user_agent": user_agent,
        },
    )


def _ensure_login_allowed(admin: AdminAccount, now: datetime) -> None:
    if admin.status != "active":
        if admin.status == "locked":
            if admin.locked_until_at is not None and admin.locked_until_at <= now:
                admin.status = "active"
                admin.locked_until_at = None
                return
            raise AccountLockedError("admin account is locked")
        raise AccountLockedError("admin account is not active")


async def _active_token(
    repository: AdminAuthRepository,
    raw_token: str,
    token_type: Literal["login_link", "refresh_token", "password_reset"],
    now: datetime,
) -> AdminAuthToken:
    token = await repository.get_auth_token_by_hash(_hash_token(raw_token))
    if (
        token is None
        or token.token_type != token_type
        or token.status != "active"
        or token.expires_at <= now
    ):
        raise AuthenticationError("admin token is invalid")
    return token


async def _active_admin(
    repository: AdminAuthRepository,
    admin_id: str,
) -> AdminAccount:
    admin = await repository.get_admin_account(admin_id)
    if admin is None:
        raise AuthenticationError("admin account is invalid")
    if admin.status != "active":
        raise ForbiddenError("admin account is not active")
    return admin


async def _issue_refresh_token(
    repository: AdminAuthRepository,
    admin: AdminAccount,
    now: datetime,
    request_ip: str | None,
    user_agent: str | None,
) -> tuple[str, datetime]:
    refresh_token = _new_token("art")
    expires_at = now + timedelta(days=REFRESH_TOKEN_TTL_DAYS)
    await repository.save_auth_token(
        AdminAuthToken(
            id=AdminAuthToken.generate_id(),
            admin_account_id=admin.id,
            token_type="refresh_token",
            token_hash=_hash_token(refresh_token),
            status="active",
            expires_at=expires_at,
            created_at=now,
            request_ip=request_ip,
            user_agent=user_agent,
        )
    )
    return refresh_token, expires_at


def _principal(admin: AdminAccount) -> AdminPrincipal:
    return AdminPrincipal(
        admin_id=admin.id,
        email=admin.email,
        display_name=admin.display_name,
        roles=admin.roles,
        permissions=admin.permissions,
        status=admin.status,
    )


def _sign_access_token(
    admin: AdminAccount,
    now: datetime,
    secret: str,
) -> str:
    payload = {
        "adminId": admin.id,
        "roles": admin.roles,
        "permissions": admin.permissions,
        "permissionVersion": admin.permission_version,
        "exp": int((now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)).timestamp()),
        "aud": "admin-console",
    }
    body = _b64(json.dumps(payload, sort_keys=True).encode("utf-8"))
    signature = _b64(
        hmac.new(
            secret.encode("utf-8"),
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return f"aat_{body}.{signature}"


def _verify_access_token(token: str, now: datetime, secret: str) -> dict[str, object]:
    if not token.startswith("aat_") or "." not in token:
        raise AuthenticationError("admin access token is invalid")
    body, signature = token.removeprefix("aat_").split(".", 1)
    expected = _b64(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected):
        raise AuthenticationError("admin access token is invalid")
    try:
        payload = json.loads(base64.urlsafe_b64decode(_pad_b64(body)).decode("utf-8"))
        expires_at = _int_claim(payload["exp"])
        permission_version = _int_claim(payload["permissionVersion"])
        admin_id = payload["adminId"]
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise AuthenticationError("admin access token is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(admin_id, str) or not admin_id:
        raise AuthenticationError("admin access token is invalid")
    if expires_at <= int(now.timestamp()):
        raise AuthenticationError("admin access token is expired")
    if payload.get("aud") != "admin-console":
        raise AuthenticationError("admin access token is invalid")
    payload["permissionVersion"] = permission_version
    return payload


def _int_claim(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise AuthenticationError("admin access token is invalid")


def _new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _pad_b64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return f"{value}{padding}".encode("ascii")
