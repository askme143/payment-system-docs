from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

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
from payments.application.ports.webhooks import WebhookRepository


class OneTimePaymentUnitOfWork(Protocol):
    idempotency_keys: IdempotencyKeyRepository
    checkouts: CheckoutRepository
    invoices: InvoiceWriteRepository
    payments: PaymentAttemptRepository
    one_time_skus: OneTimeSkuRepository
    payment_customers: PaymentCustomerRepository
    payment_cancel_requests: PaymentCancelRequestRepository
    operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class OneTimePaymentUnitOfWorkFactory(Protocol):
    def __call__(self) -> OneTimePaymentUnitOfWork:
        raise NotImplementedError


class AdminAuthUnitOfWork(Protocol):
    admin_auth: AdminAuthRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class AdminAuthUnitOfWorkFactory(Protocol):
    def __call__(self) -> AdminAuthUnitOfWork:
        raise NotImplementedError


class AdminSubscriptionAdjustUnitOfWork(Protocol):
    admin_operations: AdminOperationsRepository
    idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class AdminSubscriptionAdjustUnitOfWorkFactory(Protocol):
    def __call__(self) -> AdminSubscriptionAdjustUnitOfWork:
        raise NotImplementedError


class SubscriptionConfirmUnitOfWork(Protocol):
    billing_auths: BillingAuthRepository
    subscriptions: SubscriptionCheckoutRepository
    idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class SubscriptionConfirmUnitOfWorkFactory(Protocol):
    def __call__(self) -> SubscriptionConfirmUnitOfWork:
        raise NotImplementedError


class BillingAuthIssueUnitOfWork(Protocol):
    billing_auths: BillingAuthRepository
    idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class BillingAuthIssueUnitOfWorkFactory(Protocol):
    def __call__(self) -> BillingAuthIssueUnitOfWork:
        raise NotImplementedError


class SubscriptionCancelUnitOfWork(Protocol):
    subscriptions: SubscriptionAccountRepository
    idempotency_keys: IdempotencyKeyRepository
    operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class SubscriptionCancelUnitOfWorkFactory(Protocol):
    def __call__(self) -> SubscriptionCancelUnitOfWork:
        raise NotImplementedError


class SubscriptionResumeUnitOfWork(Protocol):
    subscriptions: SubscriptionAccountRepository
    idempotency_keys: IdempotencyKeyRepository
    operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class SubscriptionResumeUnitOfWorkFactory(Protocol):
    def __call__(self) -> SubscriptionResumeUnitOfWork:
        raise NotImplementedError


class SubscriptionExpirationUnitOfWork(Protocol):
    subscriptions: SubscriptionExpirationRepository
    operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class SubscriptionExpirationUnitOfWorkFactory(Protocol):
    def __call__(self) -> SubscriptionExpirationUnitOfWork:
        raise NotImplementedError


class SubscriptionBillingUnitOfWork(Protocol):
    billing: BillingRetryRepository
    idempotency_keys: IdempotencyKeyRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class SubscriptionBillingUnitOfWorkFactory(Protocol):
    def __call__(self) -> SubscriptionBillingUnitOfWork:
        raise NotImplementedError


class WebhookUnitOfWork(Protocol):
    webhooks: WebhookRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class WebhookUnitOfWorkFactory(Protocol):
    def __call__(self) -> WebhookUnitOfWork:
        raise NotImplementedError


class SubscriptionChangeUnitOfWork(Protocol):
    billing: BillingRetryRepository
    subscriptions: SubscriptionAccountRepository
    idempotency_keys: IdempotencyKeyRepository
    operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class SubscriptionChangeUnitOfWorkFactory(Protocol):
    def __call__(self) -> SubscriptionChangeUnitOfWork:
        raise NotImplementedError


class BillingMethodDefaultUnitOfWork(Protocol):
    billing_methods: BillingMethodRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class BillingMethodDefaultUnitOfWorkFactory(Protocol):
    def __call__(self) -> BillingMethodDefaultUnitOfWork:
        raise NotImplementedError


class BillingMethodDeleteUnitOfWork(Protocol):
    billing_methods: BillingMethodRepository
    idempotency_keys: IdempotencyKeyRepository
    operator_audits: OperatorAuditRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class BillingMethodDeleteUnitOfWorkFactory(Protocol):
    def __call__(self) -> BillingMethodDeleteUnitOfWork:
        raise NotImplementedError
