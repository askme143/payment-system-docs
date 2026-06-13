from __future__ import annotations

from datetime import timedelta
from typing import cast

from motor.motor_asyncio import AsyncIOMotorDatabase

from payments.adapters.email import JinjaTemplateRenderer, SMTPEmailSender
from payments.adapters.mongo.notifications import (
    MongoNotificationOutboxRepository,
    MongoNotificationTemplateRepository,
)
from payments.application.jobs.notifications import NotificationWorkerPolicy
from payments.scheduler.composition import build_notification_worker_dependencies
from payments.scheduler.config import payment_scheduler_config_from_env


class FakeDatabase:
    def __init__(self) -> None:
        self.notification_outbox = object()
        self.notification_templates = object()


def test_payment_scheduler_config_from_env_loads_notification_worker_values() -> None:
    config = payment_scheduler_config_from_env(
        {
            "PAYMENTS_DATABASE_URL": "mongodb://localhost:27017",
            "PAYMENTS_DATABASE_NAME": "payments_test",
            "PAYMENTS_INTERNAL_SERVICE_TOKEN": "internal-secret",
            "PAYMENTS_NOTIFICATION_TEMPLATE_ARG_ENCRYPTION_SECRET": (
                "notification-secret"
            ),
            "PAYMENTS_NOTIFICATION_WORKER_ID": "worker-a",
            "PAYMENTS_NOTIFICATION_WORKER_BATCH_SIZE": "25",
            "PAYMENTS_NOTIFICATION_WORKER_CLAIM_LIMIT_PER_RUN": "30",
            "PAYMENTS_NOTIFICATION_WORKER_BACKOFF_SECONDS": "1,5,30",
            "PAYMENTS_SMTP_HOST": "smtp.example.com",
            "PAYMENTS_SMTP_PORT": "2525",
            "PAYMENTS_SMTP_FROM_EMAIL": "payments@example.com",
            "PAYMENTS_SMTP_FROM_NAME": "Payments",
            "PAYMENTS_SMTP_USERNAME": "smtp-user",
            "PAYMENTS_SMTP_PASSWORD": "smtp-password",
            "PAYMENTS_SMTP_USE_TLS": "false",
            "PAYMENTS_SMTP_TIMEOUT_SECONDS": "3.5",
            "PAYMENTS_SMTP_REPLY_TO": "support@example.com",
        }
    )

    assert config.database_name == "payments_test"
    assert config.notification_template_arg_encryption_secret == (
        "notification-secret"
    )
    assert config.notification_worker_id == "worker-a"
    assert config.notification_worker_policy.batch_size == 25
    assert config.notification_worker_policy.claim_limit_per_run == 30
    assert config.notification_worker_policy.backoff_schedule == (
        timedelta(seconds=1),
        timedelta(seconds=5),
        timedelta(seconds=30),
    )
    assert config.smtp.host == "smtp.example.com"
    assert config.smtp.port == 2525
    assert config.smtp.from_email == "payments@example.com"
    assert config.smtp.from_name == "Payments"
    assert config.smtp.username == "smtp-user"
    assert config.smtp.password == "smtp-password"
    assert config.smtp.use_tls is False
    assert config.smtp.timeout_seconds == 3.5
    assert config.smtp.reply_to == "support@example.com"


def test_payment_scheduler_config_uses_documented_worker_defaults() -> None:
    config = payment_scheduler_config_from_env(
        {
            "PAYMENTS_DATABASE_URL": "mongodb://localhost:27017",
            "PAYMENTS_DATABASE_NAME": "payments_test",
            "PAYMENTS_INTERNAL_SERVICE_TOKEN": "internal-secret",
            "PAYMENTS_SMTP_HOST": "smtp.example.com",
            "PAYMENTS_SMTP_FROM_EMAIL": "payments@example.com",
        }
    )

    assert config.notification_worker_policy == NotificationWorkerPolicy()
    assert config.notification_template_arg_encryption_secret == "internal-secret"
    assert config.smtp.port == 587
    assert config.smtp.use_tls is True


def test_build_notification_worker_dependencies_wires_smtp_sender() -> None:
    config = payment_scheduler_config_from_env(
        {
            "PAYMENTS_DATABASE_URL": "mongodb://localhost:27017",
            "PAYMENTS_DATABASE_NAME": "payments_test",
            "PAYMENTS_INTERNAL_SERVICE_TOKEN": "internal-secret",
            "PAYMENTS_SMTP_HOST": "smtp.example.com",
            "PAYMENTS_SMTP_FROM_EMAIL": "payments@example.com",
        }
    )
    dependencies = build_notification_worker_dependencies(
        cast(AsyncIOMotorDatabase, FakeDatabase()),
        config,
    )

    assert isinstance(
        dependencies.outbox_repository,
        MongoNotificationOutboxRepository,
    )
    assert isinstance(
        dependencies.template_repository,
        MongoNotificationTemplateRepository,
    )
    assert isinstance(dependencies.email_sender, SMTPEmailSender)
    assert isinstance(dependencies.template_renderer, JinjaTemplateRenderer)
