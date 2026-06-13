from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PaymentHttpConfig:
    database_url: str
    database_name: str
    internal_service_token: str
    toss_client_key: str = "test_ck_local"
    toss_secret_key: str = ""
    toss_base_url: str = "https://api.tosspayments.com"
    toss_webhook_secret: str = ""
    billing_key_encryption_secret: str = ""
    notification_template_arg_encryption_secret: str = ""
    admin_auth_link_base_url: str = ""
    notification_recipient_api_base_url: str = ""
    allowed_redirect_hosts: tuple[str, ...] = ("example.com",)


def payment_config_from_env(
    environ: Mapping[str, str] = os.environ,
) -> PaymentHttpConfig:
    database_url = _required_env(environ, "PAYMENTS_DATABASE_URL")
    database_name = _required_env(environ, "PAYMENTS_DATABASE_NAME")
    internal_service_token = _required_env(
        environ,
        "PAYMENTS_INTERNAL_SERVICE_TOKEN",
    )
    return PaymentHttpConfig(
        database_url=database_url,
        database_name=database_name,
        internal_service_token=internal_service_token,
        toss_client_key=environ.get("PAYMENTS_TOSS_CLIENT_KEY", "test_ck_local"),
        toss_secret_key=environ.get("PAYMENTS_TOSS_SECRET_KEY", ""),
        toss_base_url=environ.get(
            "PAYMENTS_TOSS_BASE_URL",
            "https://api.tosspayments.com",
        ),
        toss_webhook_secret=_required_env(environ, "PAYMENTS_TOSS_WEBHOOK_SECRET"),
        billing_key_encryption_secret=environ.get(
            "PAYMENTS_BILLING_KEY_ENCRYPTION_SECRET",
            internal_service_token,
        ),
        notification_template_arg_encryption_secret=environ.get(
            "PAYMENTS_NOTIFICATION_TEMPLATE_ARG_ENCRYPTION_SECRET",
            internal_service_token,
        ),
        admin_auth_link_base_url=_required_env(
            environ,
            "PAYMENTS_ADMIN_AUTH_LINK_BASE_URL",
        ),
        notification_recipient_api_base_url=_required_env(
            environ,
            "PAYMENTS_NOTIFICATION_RECIPIENT_API_BASE_URL",
        ),
        allowed_redirect_hosts=_csv_tuple(
            environ.get("PAYMENTS_ALLOWED_REDIRECT_HOSTS", "example.com")
        ),
    )


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


def _csv_tuple(value: str) -> tuple[str, ...]:
    items = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError("PAYMENTS_ALLOWED_REDIRECT_HOSTS must not be empty")
    return items
