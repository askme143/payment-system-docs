from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PaymentHttpConfig:
    database_url: str
    database_name: str
    internal_service_token: str


def payment_config_from_env(
    environ: Mapping[str, str] = os.environ,
) -> PaymentHttpConfig:
    return PaymentHttpConfig(
        database_url=_required_env(environ, "PAYMENTS_DATABASE_URL"),
        database_name=_required_env(environ, "PAYMENTS_DATABASE_NAME"),
        internal_service_token=_required_env(
            environ, "PAYMENTS_INTERNAL_SERVICE_TOKEN"
        ),
    )


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value
