from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Header, Request, Response, status

from payments.application.admin_auth import (
    confirm_admin_login,
    confirm_admin_password_reset,
    get_current_admin,
    logout_admin,
    refresh_admin_token,
    request_admin_password_reset,
    start_admin_login,
)
from payments.application.errors import (
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
)
from payments.http.dependencies import HttpDependencies
from payments.http.schemas.admin_auth import (
    AdminLoginAcceptedResponse,
    AdminLoginConfirmRequest,
    AdminLoginRequest,
    AdminLogoutRequest,
    AdminPasswordResetAcceptedResponse,
    AdminPasswordResetConfirmRequest,
    AdminPasswordResetRequest,
    AdminProfileResponse,
    AdminRefreshRequest,
    AdminRefreshResponse,
    AdminTokenPairResponse,
    admin_login_accepted_response,
    admin_password_reset_accepted_response,
    admin_profile_response,
    admin_refresh_response,
    admin_token_pair_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])

    @router.post(
        "/login",
        response_model=AdminLoginAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def login(
        body: AdminLoginRequest,
        request: Request,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> AdminLoginAcceptedResponse:
        _require_request_id(x_request_id)
        result = await start_admin_login(
            email=_required_text(body.email, "email"),
            password=_required_text(body.password, "password"),
            repository=dependencies.admin_auth,
            email_sender=dependencies.admin_auth_email_sender,
            rate_limiter=dependencies.admin_auth_rate_limiter,
            clock=dependencies.clock,
            request_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=x_request_id,
        )
        return admin_login_accepted_response(result)

    @router.post("/login/confirm", response_model=AdminTokenPairResponse)
    async def login_confirm(
        body: AdminLoginConfirmRequest,
        request: Request,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> AdminTokenPairResponse:
        _require_request_id(x_request_id)
        result = await confirm_admin_login(
            login_token=_required_text(body.login_token, "loginToken"),
            repository=dependencies.admin_auth,
            admin_auth_uow_factory=dependencies.admin_auth_uow_factory,
            clock=dependencies.clock,
            access_token_secret=dependencies.internal_service_token,
            request_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=x_request_id,
        )
        return admin_token_pair_response(result)

    @router.get("/me", response_model=AdminProfileResponse)
    async def me(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> AdminProfileResponse:
        _require_request_id(x_request_id)
        admin = await get_current_admin(
            admin_access_token=_bearer_token(authorization),
            repository=dependencies.admin_auth,
            clock=dependencies.clock,
            access_token_secret=dependencies.internal_service_token,
        )
        return admin_profile_response(admin)

    @router.post("/refresh", response_model=AdminRefreshResponse)
    async def refresh(
        body: AdminRefreshRequest,
        request: Request,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> AdminRefreshResponse:
        _require_request_id(x_request_id)
        result = await refresh_admin_token(
            admin_refresh_token=_required_text(
                body.admin_refresh_token,
                "adminRefreshToken",
            ),
            repository=dependencies.admin_auth,
            admin_auth_uow_factory=dependencies.admin_auth_uow_factory,
            clock=dependencies.clock,
            access_token_secret=dependencies.internal_service_token,
            request_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        return admin_refresh_response(result)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        body: Annotated[AdminLogoutRequest | None, Body()] = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> Response:
        _require_request_id(x_request_id)
        await logout_admin(
            admin_access_token=_bearer_token(authorization),
            admin_refresh_token=_logout_refresh_token(body),
            repository=dependencies.admin_auth,
            clock=dependencies.clock,
            access_token_secret=dependencies.internal_service_token,
            request_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=x_request_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/password-reset/request",
        response_model=AdminPasswordResetAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def password_reset_request(
        body: AdminPasswordResetRequest,
        request: Request,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> AdminPasswordResetAcceptedResponse:
        _require_request_id(x_request_id)
        result = await request_admin_password_reset(
            email=_required_text(body.email, "email"),
            repository=dependencies.admin_auth,
            email_sender=dependencies.admin_auth_email_sender,
            rate_limiter=dependencies.admin_auth_rate_limiter,
            clock=dependencies.clock,
            request_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        return admin_password_reset_accepted_response(result)

    @router.post(
        "/password-reset/confirm",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def password_reset_confirm(
        body: AdminPasswordResetConfirmRequest,
        request: Request,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> Response:
        _require_request_id(x_request_id)
        await confirm_admin_password_reset(
            reset_token=_required_text(body.reset_token, "resetToken"),
            new_password=_required_text(body.new_password, "newPassword"),
            repository=dependencies.admin_auth,
            admin_auth_uow_factory=dependencies.admin_auth_uow_factory,
            clock=dependencies.clock,
            request_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=x_request_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _require_request_id(value: str | None) -> None:
    if value is None or not value.strip():
        raise AuthorizationError("X-Request-Id header is required")


def _bearer_token(value: str | None) -> str:
    prefix = "Bearer "
    if value is None or not value.startswith(prefix):
        raise AuthenticationError("admin access token is required")
    return value.removeprefix(prefix)


def _required_text(value: object | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value


def _logout_refresh_token(body: AdminLogoutRequest | None) -> str | None:
    if body is None or "admin_refresh_token" not in body.model_fields_set:
        return None
    if not isinstance(body.admin_refresh_token, str) or not body.admin_refresh_token:
        return ""
    return body.admin_refresh_token


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None
