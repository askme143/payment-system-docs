from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from payments.application.admin_auth import (
    AdminLoginAccepted,
    AdminPrincipal,
    AdminRefreshResult,
    AdminTokenPair,
)


class AdminLoginRequest(BaseModel):
    email: object | None = None
    password: object | None = None


class AdminLoginAcceptedResponse(BaseModel):
    accepted: bool
    delivery: str
    expires_in_seconds: int = Field(alias="expiresInSeconds")


class AdminLoginConfirmRequest(BaseModel):
    login_token: object | None = Field(default=None, alias="loginToken")


class AdminProfileResponse(BaseModel):
    admin_id: str = Field(alias="adminId")
    email: str
    display_name: str = Field(alias="displayName")
    roles: list[str]
    permissions: list[str]
    status: str | None = None


class AdminSessionProfileResponse(BaseModel):
    admin_id: str = Field(alias="adminId")
    email: str
    display_name: str = Field(alias="displayName")
    roles: list[str]
    permissions: list[str]


class AdminTokenPairResponse(BaseModel):
    admin: AdminSessionProfileResponse
    admin_access_token: str = Field(alias="adminAccessToken")
    expires_in_seconds: int = Field(alias="expiresInSeconds")
    admin_refresh_token: str = Field(alias="adminRefreshToken")
    refresh_expires_at: datetime = Field(alias="refreshExpiresAt")


class AdminRefreshRequest(BaseModel):
    admin_refresh_token: object | None = Field(default=None, alias="adminRefreshToken")


class AdminRefreshResponse(BaseModel):
    admin_access_token: str = Field(alias="adminAccessToken")
    expires_in_seconds: int = Field(alias="expiresInSeconds")
    admin_refresh_token: str = Field(alias="adminRefreshToken")
    refresh_expires_at: datetime = Field(alias="refreshExpiresAt")


class AdminLogoutRequest(BaseModel):
    admin_refresh_token: object | None = Field(default=None, alias="adminRefreshToken")


class AdminPasswordResetRequest(BaseModel):
    email: object | None = None


class AdminPasswordResetAcceptedResponse(BaseModel):
    accepted: bool


class AdminPasswordResetConfirmRequest(BaseModel):
    reset_token: object | None = Field(default=None, alias="resetToken")
    new_password: object | None = Field(default=None, alias="newPassword")


def admin_login_accepted_response(
    result: AdminLoginAccepted,
) -> AdminLoginAcceptedResponse:
    return AdminLoginAcceptedResponse(
        accepted=result.accepted,
        delivery=result.delivery,
        expiresInSeconds=result.expires_in_seconds,
    )


def admin_password_reset_accepted_response(
    result: AdminLoginAccepted,
) -> AdminPasswordResetAcceptedResponse:
    return AdminPasswordResetAcceptedResponse(accepted=result.accepted)


def admin_profile_response(admin: AdminPrincipal) -> AdminProfileResponse:
    return AdminProfileResponse(
        adminId=admin.admin_id,
        email=admin.email,
        displayName=admin.display_name,
        roles=admin.roles,
        permissions=admin.permissions,
        status=admin.status,
    )


def admin_token_pair_response(result: AdminTokenPair) -> AdminTokenPairResponse:
    return AdminTokenPairResponse(
        admin=AdminSessionProfileResponse(
            adminId=result.admin.admin_id,
            email=result.admin.email,
            displayName=result.admin.display_name,
            roles=result.admin.roles,
            permissions=result.admin.permissions,
        ),
        adminAccessToken=result.admin_access_token,
        expiresInSeconds=result.expires_in_seconds,
        adminRefreshToken=result.admin_refresh_token,
        refreshExpiresAt=result.refresh_expires_at,
    )


def admin_refresh_response(result: AdminRefreshResult) -> AdminRefreshResponse:
    return AdminRefreshResponse(
        adminAccessToken=result.admin_access_token,
        expiresInSeconds=result.expires_in_seconds,
        adminRefreshToken=result.admin_refresh_token,
        refreshExpiresAt=result.refresh_expires_at,
    )
