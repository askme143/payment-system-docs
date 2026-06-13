from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from payments.adapters.email import SMTPEmailSenderConfig
from payments.application.jobs.notifications import NotificationWorkerPolicy


@dataclass(frozen=True, slots=True)
class PaymentSchedulerConfig:
    database_url: str
    database_name: str
    internal_service_token: str
    notification_template_arg_encryption_secret: str
    notification_worker_id: str
    notification_worker_policy: NotificationWorkerPolicy
    smtp: SMTPEmailSenderConfig


def payment_scheduler_config_from_env(
    environ: Mapping[str, str] = os.environ,
) -> PaymentSchedulerConfig:
    internal_service_token = _required_env(
        environ,
        "PAYMENTS_INTERNAL_SERVICE_TOKEN",
    )
    return PaymentSchedulerConfig(
        database_url=_required_env(environ, "PAYMENTS_DATABASE_URL"),
        database_name=_required_env(environ, "PAYMENTS_DATABASE_NAME"),
        internal_service_token=internal_service_token,
        notification_template_arg_encryption_secret=environ.get(
            "PAYMENTS_NOTIFICATION_TEMPLATE_ARG_ENCRYPTION_SECRET",
            internal_service_token,
        ),
        notification_worker_id=environ.get(
            "PAYMENTS_NOTIFICATION_WORKER_ID",
            "notification-worker",
        ),
        notification_worker_policy=NotificationWorkerPolicy(
            batch_size=_optional_positive_int(
                environ,
                "PAYMENTS_NOTIFICATION_WORKER_BATCH_SIZE",
                100,
            ),
            claim_limit_per_run=_optional_positive_int(
                environ,
                "PAYMENTS_NOTIFICATION_WORKER_CLAIM_LIMIT_PER_RUN",
                100,
            ),
            poll_interval=timedelta(
                seconds=_optional_positive_float(
                    environ,
                    "PAYMENTS_NOTIFICATION_WORKER_POLL_INTERVAL_SECONDS",
                    10.0,
                )
            ),
            lock_duration=timedelta(
                seconds=_optional_positive_float(
                    environ,
                    "PAYMENTS_NOTIFICATION_WORKER_LOCK_DURATION_SECONDS",
                    300.0,
                )
            ),
            max_attempts=_optional_positive_int(
                environ,
                "PAYMENTS_NOTIFICATION_WORKER_MAX_ATTEMPTS",
                5,
            ),
            backoff_schedule=tuple(
                timedelta(seconds=value)
                for value in _optional_positive_float_tuple(
                    environ,
                    "PAYMENTS_NOTIFICATION_WORKER_BACKOFF_SECONDS",
                    (60.0, 300.0, 1_800.0, 7_200.0, 43_200.0),
                )
            ),
        ),
        smtp=SMTPEmailSenderConfig(
            host=_required_env(environ, "PAYMENTS_SMTP_HOST"),
            port=_optional_positive_int(environ, "PAYMENTS_SMTP_PORT", 587),
            from_email=_required_env(environ, "PAYMENTS_SMTP_FROM_EMAIL"),
            from_name=environ.get("PAYMENTS_SMTP_FROM_NAME") or None,
            username=environ.get("PAYMENTS_SMTP_USERNAME") or None,
            password=environ.get("PAYMENTS_SMTP_PASSWORD") or None,
            use_tls=_optional_bool(environ, "PAYMENTS_SMTP_USE_TLS", True),
            timeout_seconds=_optional_positive_float(
                environ,
                "PAYMENTS_SMTP_TIMEOUT_SECONDS",
                10.0,
            ),
            reply_to=environ.get("PAYMENTS_SMTP_REPLY_TO") or None,
        ),
    )


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


def _optional_positive_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw_value = environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_positive_float(
    environ: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    raw_value = environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_positive_float_tuple(
    environ: Mapping[str, str],
    name: str,
    default: tuple[float, ...],
) -> tuple[float, ...]:
    raw_value = environ.get(name)
    if raw_value is None:
        return default
    values = tuple(
        float(item.strip())
        for item in raw_value.split(",")
        if item.strip()
    )
    if not values or any(value <= 0 for value in values):
        raise ValueError(f"{name} must contain positive numbers")
    return values


def _optional_bool(
    environ: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    raw_value = environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
