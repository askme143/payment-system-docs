from __future__ import annotations

from payments.adapters.mongo.admin_auth import MongoAdminAuthRepository
from payments.adapters.mongo.admin_catalog import MongoAdminCatalogRepository
from payments.adapters.mongo.admin_operations import MongoAdminOperationsRepository
from payments.adapters.mongo.billing_auth import MongoBillingAuthRepository
from payments.adapters.mongo.billing_methods import MongoBillingMethodRepository
from payments.adapters.mongo.billing_retry import MongoBillingRetryRepository
from payments.adapters.mongo.catalog import MongoCatalogRepository
from payments.adapters.mongo.checkouts import MongoCheckoutRepository
from payments.adapters.mongo.idempotency import MongoIdempotencyKeyRepository
from payments.adapters.mongo.indexes import ensure_mongo_indexes
from payments.adapters.mongo.invoices import MongoInvoiceRepository
from payments.adapters.mongo.notifications import (
    MongoNotificationOutboxRepository,
    MongoNotificationTemplateRepository,
)
from payments.adapters.mongo.one_time_skus import MongoOneTimeSkuRepository
from payments.adapters.mongo.operation_locks import MongoOperationLockRepository
from payments.adapters.mongo.operator_audits import MongoOperatorAuditRepository
from payments.adapters.mongo.payment_attempts import MongoPaymentAttemptRepository
from payments.adapters.mongo.payment_cancel_requests import (
    MongoPaymentCancelRequestRepository,
)
from payments.adapters.mongo.payment_customers import MongoPaymentCustomerRepository
from payments.adapters.mongo.subscriptions import (
    MongoSubscriptionAccountRepository,
    MongoSubscriptionCheckoutRepository,
    MongoSubscriptionExpirationRepository,
)
from payments.adapters.mongo.unit_of_work import (
    MongoAdminAuthUnitOfWorkFactory,
    MongoAdminSubscriptionAdjustUnitOfWorkFactory,
    MongoBillingAuthIssueUnitOfWorkFactory,
    MongoBillingMethodDefaultUnitOfWorkFactory,
    MongoBillingMethodDeleteUnitOfWorkFactory,
    MongoOneTimePaymentUnitOfWorkFactory,
    MongoSubscriptionBillingUnitOfWorkFactory,
    MongoSubscriptionCancelUnitOfWorkFactory,
    MongoSubscriptionChangeUnitOfWorkFactory,
    MongoSubscriptionConfirmUnitOfWorkFactory,
    MongoSubscriptionExpirationUnitOfWorkFactory,
    MongoSubscriptionResumeUnitOfWorkFactory,
    MongoWebhookUnitOfWorkFactory,
)
from payments.adapters.mongo.webhooks import MongoWebhookRepository

__all__ = [
    "MongoAdminAuthRepository",
    "MongoAdminAuthUnitOfWorkFactory",
    "MongoAdminCatalogRepository",
    "MongoAdminOperationsRepository",
    "MongoAdminSubscriptionAdjustUnitOfWorkFactory",
    "MongoBillingAuthIssueUnitOfWorkFactory",
    "MongoBillingAuthRepository",
    "MongoBillingMethodDefaultUnitOfWorkFactory",
    "MongoBillingMethodDeleteUnitOfWorkFactory",
    "MongoBillingMethodRepository",
    "MongoBillingRetryRepository",
    "MongoCatalogRepository",
    "MongoCheckoutRepository",
    "MongoIdempotencyKeyRepository",
    "MongoInvoiceRepository",
    "MongoNotificationOutboxRepository",
    "MongoNotificationTemplateRepository",
    "MongoOneTimePaymentUnitOfWorkFactory",
    "MongoOneTimeSkuRepository",
    "MongoOperationLockRepository",
    "MongoOperatorAuditRepository",
    "MongoPaymentAttemptRepository",
    "MongoPaymentCancelRequestRepository",
    "MongoPaymentCustomerRepository",
    "MongoSubscriptionAccountRepository",
    "MongoSubscriptionBillingUnitOfWorkFactory",
    "MongoSubscriptionCancelUnitOfWorkFactory",
    "MongoSubscriptionChangeUnitOfWorkFactory",
    "MongoSubscriptionCheckoutRepository",
    "MongoSubscriptionConfirmUnitOfWorkFactory",
    "MongoSubscriptionExpirationRepository",
    "MongoSubscriptionExpirationUnitOfWorkFactory",
    "MongoSubscriptionResumeUnitOfWorkFactory",
    "MongoWebhookRepository",
    "MongoWebhookUnitOfWorkFactory",
    "ensure_mongo_indexes",
]
