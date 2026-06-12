from __future__ import annotations

from types import TracebackType

from motor.motor_asyncio import (
    AsyncIOMotorClientSession,
    AsyncIOMotorDatabase,
)

from payments.adapters.mongo.admin_auth import MongoAdminAuthRepository
from payments.adapters.mongo.admin_operations import MongoAdminOperationsRepository
from payments.adapters.mongo.billing_auth import MongoBillingAuthRepository
from payments.adapters.mongo.billing_methods import MongoBillingMethodRepository
from payments.adapters.mongo.billing_retry import MongoBillingRetryRepository
from payments.adapters.mongo.checkouts import MongoCheckoutRepository
from payments.adapters.mongo.idempotency import MongoIdempotencyKeyRepository
from payments.adapters.mongo.invoices import MongoInvoiceRepository
from payments.adapters.mongo.one_time_skus import MongoOneTimeSkuRepository
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
from payments.adapters.mongo.webhooks import MongoWebhookRepository
from payments.application.ports.admin_auth import AdminAuthRepository
from payments.application.ports.admin_operations import AdminOperationsRepository
from payments.application.ports.billing_auth import BillingAuthRepository
from payments.application.ports.billing_methods import BillingMethodRepository
from payments.application.ports.billing_retry import BillingRetryRepository
from payments.application.ports.checkouts import CheckoutRepository
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.invoices import InvoiceWriteRepository
from payments.application.ports.one_time_skus import OneTimeSkuRepository
from payments.application.ports.operator_audits import OperatorAuditRepository
from payments.application.ports.payment_attempts import PaymentAttemptRepository
from payments.application.ports.payment_cancel_requests import (
    PaymentCancelRequestRepository,
)
from payments.application.ports.payment_customers import PaymentCustomerRepository
from payments.application.ports.subscriptions import (
    SubscriptionAccountRepository,
    SubscriptionCheckoutRepository,
    SubscriptionExpirationRepository,
)
from payments.application.ports.unit_of_work import (
    AdminAuthUnitOfWork,
    AdminAuthUnitOfWorkFactory,
    AdminSubscriptionAdjustUnitOfWork,
    AdminSubscriptionAdjustUnitOfWorkFactory,
    BillingAuthIssueUnitOfWork,
    BillingAuthIssueUnitOfWorkFactory,
    BillingMethodDefaultUnitOfWork,
    BillingMethodDefaultUnitOfWorkFactory,
    BillingMethodDeleteUnitOfWork,
    BillingMethodDeleteUnitOfWorkFactory,
    OneTimePaymentUnitOfWork,
    OneTimePaymentUnitOfWorkFactory,
    SubscriptionBillingUnitOfWork,
    SubscriptionBillingUnitOfWorkFactory,
    SubscriptionCancelUnitOfWork,
    SubscriptionCancelUnitOfWorkFactory,
    SubscriptionChangeUnitOfWork,
    SubscriptionChangeUnitOfWorkFactory,
    SubscriptionConfirmUnitOfWork,
    SubscriptionConfirmUnitOfWorkFactory,
    SubscriptionExpirationUnitOfWork,
    SubscriptionExpirationUnitOfWorkFactory,
    SubscriptionResumeUnitOfWork,
    SubscriptionResumeUnitOfWorkFactory,
    WebhookUnitOfWork,
    WebhookUnitOfWorkFactory,
)
from payments.application.ports.webhooks import WebhookRepository


class MongoOneTimePaymentUnitOfWork(OneTimePaymentUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.idempotency_keys: IdempotencyKeyRepository
        self.checkouts: CheckoutRepository
        self.invoices: InvoiceWriteRepository
        self.payments: PaymentAttemptRepository
        self.one_time_skus: OneTimeSkuRepository
        self.payment_customers: PaymentCustomerRepository
        self.payment_cancel_requests: PaymentCancelRequestRepository
        self.operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> MongoOneTimePaymentUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        self.checkouts = MongoCheckoutRepository(
            self._database.checkouts,
            session=self._session,
        )
        self.invoices = MongoInvoiceRepository(
            invoices=self._database.invoices,
            payments=self._database.payments,
            subscriptions=self._database.subscriptions,
            subscription_plans=self._database.subscription_plans,
            products=self._database.products,
            session=self._session,
        )
        self.payments = MongoPaymentAttemptRepository(
            self._database.checkouts,
            self._database.payments,
            session=self._session,
        )
        self.one_time_skus = MongoOneTimeSkuRepository(
            self._database.products,
            self._database.one_time_skus,
            session=self._session,
        )
        self.payment_customers = MongoPaymentCustomerRepository(
            self._database.payment_customers,
            session=self._session,
        )
        self.payment_cancel_requests = MongoPaymentCancelRequestRepository(
            self._database.payment_cancel_requests,
            session=self._session,
        )
        self.operator_audits = MongoOperatorAuditRepository(
            self._database.operator_audits,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoOneTimePaymentUnitOfWorkFactory(OneTimePaymentUnitOfWorkFactory):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoOneTimePaymentUnitOfWork:
        return MongoOneTimePaymentUnitOfWork(self._database)


class MongoAdminAuthUnitOfWork(AdminAuthUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.admin_auth: AdminAuthRepository

    async def __aenter__(self) -> MongoAdminAuthUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.admin_auth = MongoAdminAuthRepository(
            admin_accounts=self._database.admin_accounts,
            admin_auth_tokens=self._database.admin_auth_tokens,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoAdminAuthUnitOfWorkFactory(AdminAuthUnitOfWorkFactory):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoAdminAuthUnitOfWork:
        return MongoAdminAuthUnitOfWork(self._database)


class MongoAdminSubscriptionAdjustUnitOfWork(AdminSubscriptionAdjustUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.admin_operations: AdminOperationsRepository
        self.idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> MongoAdminSubscriptionAdjustUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.admin_operations = MongoAdminOperationsRepository(
            payments=self._database.payments,
            invoices=self._database.invoices,
            checkouts=self._database.checkouts,
            subscriptions=self._database.subscriptions,
            subscription_plans=self._database.subscription_plans,
            products=self._database.products,
            billing_methods=self._database.billing_methods,
            operator_audits=self._database.operator_audits,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoAdminSubscriptionAdjustUnitOfWorkFactory(
    AdminSubscriptionAdjustUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoAdminSubscriptionAdjustUnitOfWork:
        return MongoAdminSubscriptionAdjustUnitOfWork(self._database)


class MongoSubscriptionConfirmUnitOfWork(SubscriptionConfirmUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.billing_auths: BillingAuthRepository
        self.subscriptions: SubscriptionCheckoutRepository
        self.idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> MongoSubscriptionConfirmUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.billing_auths = MongoBillingAuthRepository(
            billing_auths=self._database.billing_auths,
            payment_customers=self._database.payment_customers,
            billing_methods=self._database.billing_methods,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.subscriptions = MongoSubscriptionCheckoutRepository(
            subscriptions=self._database.subscriptions,
            payments=self._database.payments,
            invoices=self._database.invoices,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoSubscriptionConfirmUnitOfWorkFactory(
    SubscriptionConfirmUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoSubscriptionConfirmUnitOfWork:
        return MongoSubscriptionConfirmUnitOfWork(self._database)


class MongoBillingAuthIssueUnitOfWork(BillingAuthIssueUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.billing_auths: BillingAuthRepository
        self.idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> MongoBillingAuthIssueUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.billing_auths = MongoBillingAuthRepository(
            billing_auths=self._database.billing_auths,
            payment_customers=self._database.payment_customers,
            billing_methods=self._database.billing_methods,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoBillingAuthIssueUnitOfWorkFactory(BillingAuthIssueUnitOfWorkFactory):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoBillingAuthIssueUnitOfWork:
        return MongoBillingAuthIssueUnitOfWork(self._database)


class MongoSubscriptionCancelUnitOfWork(SubscriptionCancelUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.subscriptions: SubscriptionAccountRepository
        self.idempotency_keys: IdempotencyKeyRepository
        self.operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> MongoSubscriptionCancelUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.subscriptions = MongoSubscriptionAccountRepository(
            subscriptions=self._database.subscriptions,
            subscription_plans=self._database.subscription_plans,
            products=self._database.products,
            billing_methods=self._database.billing_methods,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        self.operator_audits = MongoOperatorAuditRepository(
            self._database.operator_audits,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoSubscriptionCancelUnitOfWorkFactory(
    SubscriptionCancelUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoSubscriptionCancelUnitOfWork:
        return MongoSubscriptionCancelUnitOfWork(self._database)


class MongoSubscriptionChangeUnitOfWork(SubscriptionChangeUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.billing: BillingRetryRepository
        self.subscriptions: SubscriptionAccountRepository
        self.idempotency_keys: IdempotencyKeyRepository
        self.operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> MongoSubscriptionChangeUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.billing = MongoBillingRetryRepository(
            invoices=self._database.invoices,
            payments=self._database.payments,
            subscriptions=self._database.subscriptions,
            subscription_plans=self._database.subscription_plans,
            billing_methods=self._database.billing_methods,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.subscriptions = MongoSubscriptionAccountRepository(
            subscriptions=self._database.subscriptions,
            subscription_plans=self._database.subscription_plans,
            products=self._database.products,
            billing_methods=self._database.billing_methods,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        self.operator_audits = MongoOperatorAuditRepository(
            self._database.operator_audits,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoSubscriptionChangeUnitOfWorkFactory(
    SubscriptionChangeUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoSubscriptionChangeUnitOfWork:
        return MongoSubscriptionChangeUnitOfWork(self._database)


class MongoSubscriptionBillingUnitOfWork(SubscriptionBillingUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.billing: BillingRetryRepository
        self.idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> MongoSubscriptionBillingUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.billing = MongoBillingRetryRepository(
            invoices=self._database.invoices,
            payments=self._database.payments,
            subscriptions=self._database.subscriptions,
            subscription_plans=self._database.subscription_plans,
            billing_methods=self._database.billing_methods,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoSubscriptionBillingUnitOfWorkFactory(
    SubscriptionBillingUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoSubscriptionBillingUnitOfWork:
        return MongoSubscriptionBillingUnitOfWork(self._database)


class MongoWebhookUnitOfWork(WebhookUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.webhooks: WebhookRepository

    async def __aenter__(self) -> MongoWebhookUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.webhooks = MongoWebhookRepository(
            webhook_events=self._database.webhook_events,
            payments=self._database.payments,
            checkouts=self._database.checkouts,
            one_time_skus=self._database.one_time_skus,
            invoices=self._database.invoices,
            subscriptions=self._database.subscriptions,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoWebhookUnitOfWorkFactory(WebhookUnitOfWorkFactory):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoWebhookUnitOfWork:
        return MongoWebhookUnitOfWork(self._database)


class MongoSubscriptionResumeUnitOfWork(SubscriptionResumeUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.subscriptions: SubscriptionAccountRepository
        self.idempotency_keys: IdempotencyKeyRepository
        self.operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> MongoSubscriptionResumeUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.subscriptions = MongoSubscriptionAccountRepository(
            subscriptions=self._database.subscriptions,
            subscription_plans=self._database.subscription_plans,
            products=self._database.products,
            billing_methods=self._database.billing_methods,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        self.operator_audits = MongoOperatorAuditRepository(
            self._database.operator_audits,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoSubscriptionResumeUnitOfWorkFactory(
    SubscriptionResumeUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoSubscriptionResumeUnitOfWork:
        return MongoSubscriptionResumeUnitOfWork(self._database)


class MongoSubscriptionExpirationUnitOfWork(SubscriptionExpirationUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.subscriptions: SubscriptionExpirationRepository
        self.operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> MongoSubscriptionExpirationUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.subscriptions = MongoSubscriptionExpirationRepository(
            subscriptions=self._database.subscriptions,
            session=self._session,
        )
        self.operator_audits = MongoOperatorAuditRepository(
            self._database.operator_audits,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoSubscriptionExpirationUnitOfWorkFactory(
    SubscriptionExpirationUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoSubscriptionExpirationUnitOfWork:
        return MongoSubscriptionExpirationUnitOfWork(self._database)


class MongoBillingMethodDefaultUnitOfWork(BillingMethodDefaultUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.billing_methods: BillingMethodRepository

    async def __aenter__(self) -> MongoBillingMethodDefaultUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.billing_methods = MongoBillingMethodRepository(
            billing_methods=self._database.billing_methods,
            subscriptions=self._database.subscriptions,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoBillingMethodDefaultUnitOfWorkFactory(
    BillingMethodDefaultUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoBillingMethodDefaultUnitOfWork:
        return MongoBillingMethodDefaultUnitOfWork(self._database)


class MongoBillingMethodDeleteUnitOfWork(BillingMethodDeleteUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.billing_methods: BillingMethodRepository
        self.idempotency_keys: IdempotencyKeyRepository
        self.operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> MongoBillingMethodDeleteUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.billing_methods = MongoBillingMethodRepository(
            billing_methods=self._database.billing_methods,
            subscriptions=self._database.subscriptions,
            payment_instruments=self._database.payment_instruments,
            session=self._session,
        )
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        self.operator_audits = MongoOperatorAuditRepository(
            self._database.operator_audits,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoBillingMethodDeleteUnitOfWorkFactory(
    BillingMethodDeleteUnitOfWorkFactory
):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoBillingMethodDeleteUnitOfWork:
        return MongoBillingMethodDeleteUnitOfWork(self._database)
