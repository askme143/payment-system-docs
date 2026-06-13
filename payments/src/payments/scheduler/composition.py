from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from payments.adapters.crypto import FernetTemplateArgCipher
from payments.adapters.email import JinjaTemplateRenderer, SMTPEmailSender
from payments.adapters.mongo.notifications import (
    MongoNotificationOutboxRepository,
    MongoNotificationTemplateRepository,
)
from payments.adapters.time import SystemClock
from payments.scheduler.config import PaymentSchedulerConfig
from payments.scheduler.notification_worker import NotificationWorkerDependencies


def create_scheduler_mongo_client(
    config: PaymentSchedulerConfig,
) -> AsyncIOMotorClient:
    return AsyncIOMotorClient(config.database_url)


def scheduler_database(
    client: AsyncIOMotorClient,
    config: PaymentSchedulerConfig,
) -> AsyncIOMotorDatabase:
    return client[config.database_name]


def build_notification_worker_dependencies(
    database: AsyncIOMotorDatabase,
    config: PaymentSchedulerConfig,
) -> NotificationWorkerDependencies:
    return NotificationWorkerDependencies(
        outbox_repository=MongoNotificationOutboxRepository(
            database.notification_outbox
        ),
        template_repository=MongoNotificationTemplateRepository(
            database.notification_templates
        ),
        email_sender=SMTPEmailSender(config.smtp),
        template_arg_cipher=FernetTemplateArgCipher(
            config.notification_template_arg_encryption_secret
        ),
        template_renderer=JinjaTemplateRenderer(),
        clock=SystemClock(),
    )
