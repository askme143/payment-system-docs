from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from payments.adapters.crypto import FernetBillingKeyCipher, FernetTemplateArgCipher
from payments.adapters.email import JinjaTemplateRenderer, SMTPEmailSender
from payments.adapters.mongo.billing_retry import MongoBillingRetryRepository
from payments.adapters.mongo.idempotency import MongoIdempotencyKeyRepository
from payments.adapters.mongo.notifications import (
    MongoNotificationOutboxRepository,
    MongoNotificationTemplateRepository,
)
from payments.adapters.mongo.operation_locks import MongoOperationLockRepository
from payments.adapters.mongo.operator_audits import MongoOperatorAuditRepository
from payments.adapters.mongo.payment_customers import MongoPaymentCustomerRepository
from payments.adapters.mongo.scheduler_runs import MongoSchedulerRunLogRepository
from payments.adapters.mongo.subscriptions import MongoSubscriptionExpirationRepository
from payments.adapters.mongo.unit_of_work import (
    MongoSubscriptionBillingUnitOfWorkFactory,
    MongoSubscriptionExpirationUnitOfWorkFactory,
)
from payments.adapters.notifications import HttpNotificationRecipientResolver
from payments.adapters.time import SystemClock
from payments.adapters.toss import TossPaymentProvider
from payments.application.notifications import NotificationEnqueueDependencies
from payments.scheduler.config import PaymentSchedulerConfig
from payments.scheduler.notification_worker import NotificationWorkerDependencies


@dataclass(frozen=True, slots=True)
class SchedulerBatchDependencies:
    billing_retries: MongoBillingRetryRepository
    payment_customers: MongoPaymentCustomerRepository
    idempotency_keys: MongoIdempotencyKeyRepository
    payment_provider: TossPaymentProvider
    clock: SystemClock
    billing_key_cipher: FernetBillingKeyCipher
    operation_locks: MongoOperationLockRepository
    subscription_billing_uow_factory: MongoSubscriptionBillingUnitOfWorkFactory
    subscription_expirations: MongoSubscriptionExpirationRepository
    subscription_expiration_uow_factory: MongoSubscriptionExpirationUnitOfWorkFactory
    operator_audits: MongoOperatorAuditRepository
    notification_enqueue: NotificationEnqueueDependencies
    scheduler_runs: MongoSchedulerRunLogRepository


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


def build_scheduler_batch_dependencies(
    database: AsyncIOMotorDatabase,
    config: PaymentSchedulerConfig,
) -> SchedulerBatchDependencies:
    notification_outbox = MongoNotificationOutboxRepository(
        database.notification_outbox
    )
    notification_templates = MongoNotificationTemplateRepository(
        database.notification_templates
    )
    notification_template_arg_cipher = FernetTemplateArgCipher(
        config.notification_template_arg_encryption_secret
    )
    clock = SystemClock()
    return SchedulerBatchDependencies(
        billing_retries=MongoBillingRetryRepository(
            invoices=database.invoices,
            payments=database.payments,
            subscriptions=database.subscriptions,
            subscription_plans=database.subscription_plans,
            billing_methods=database.billing_methods,
            payment_instruments=database.payment_instruments,
        ),
        payment_customers=MongoPaymentCustomerRepository(database.payment_customers),
        idempotency_keys=MongoIdempotencyKeyRepository(database.idempotency_keys),
        payment_provider=TossPaymentProvider(
            secret_key=config.toss_secret_key,
            base_url=config.toss_base_url,
        ),
        clock=clock,
        billing_key_cipher=FernetBillingKeyCipher(
            config.billing_key_encryption_secret
        ),
        operation_locks=MongoOperationLockRepository(
            operation_locks=database.operation_locks,
            operation_lock_counters=database.operation_lock_counters,
        ),
        subscription_billing_uow_factory=MongoSubscriptionBillingUnitOfWorkFactory(
            database,
        ),
        subscription_expirations=MongoSubscriptionExpirationRepository(
            database.subscriptions,
        ),
        subscription_expiration_uow_factory=(
            MongoSubscriptionExpirationUnitOfWorkFactory(database)
        ),
        operator_audits=MongoOperatorAuditRepository(database.operator_audits),
        notification_enqueue=NotificationEnqueueDependencies(
            outbox_repository=notification_outbox,
            template_repository=notification_templates,
            recipient_resolver=HttpNotificationRecipientResolver(
                recipient_api_base_url=config.notification_recipient_api_base_url,
                admin_accounts=database.admin_accounts,
            ),
            template_arg_cipher=notification_template_arg_cipher,
            clock=clock,
        ),
        scheduler_runs=MongoSchedulerRunLogRepository(database.scheduler_run_logs),
    )
