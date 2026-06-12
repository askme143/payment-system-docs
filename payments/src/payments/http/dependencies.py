from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, Request

from payments.application.admin_auth import get_current_admin
from payments.application.admin_catalog import AdminRequestContext
from payments.application.context import RequestContext
from payments.application.errors import (
    AuthenticationError,
    AuthorizationError,
    ForbiddenError,
)
from payments.application.ports import (
    AdminAuthEmailSender,
    AdminAuthRateLimiter,
    AdminAuthRepository,
    AdminAuthUnitOfWorkFactory,
    AdminCatalogRepository,
    AdminOperationsRepository,
    AdminSubscriptionAdjustUnitOfWorkFactory,
    BillingAuthIssueUnitOfWorkFactory,
    BillingAuthRepository,
    BillingKeyCipher,
    BillingMethodDefaultUnitOfWorkFactory,
    BillingMethodDeleteUnitOfWorkFactory,
    BillingMethodRepository,
    BillingRetryRepository,
    CatalogRepository,
    Clock,
    IdempotencyKeyRepository,
    InvoiceRepository,
    OneTimePaymentUnitOfWorkFactory,
    OperationLockRepository,
    PaymentAttemptRepository,
    PaymentCustomerRepository,
    PaymentProvider,
    SubscriptionAccountRepository,
    SubscriptionBillingUnitOfWorkFactory,
    SubscriptionCancelUnitOfWorkFactory,
    SubscriptionChangeTokenCodec,
    SubscriptionChangeUnitOfWorkFactory,
    SubscriptionCheckoutRepository,
    SubscriptionConfirmUnitOfWorkFactory,
    SubscriptionExpirationRepository,
    SubscriptionExpirationUnitOfWorkFactory,
    SubscriptionResumeUnitOfWorkFactory,
    WebhookRepository,
    WebhookUnitOfWorkFactory,
)


@dataclass(frozen=True, slots=True)
class HttpDependencies:
    admin_catalog: AdminCatalogRepository
    admin_auth: AdminAuthRepository
    admin_auth_uow_factory: AdminAuthUnitOfWorkFactory
    admin_auth_email_sender: AdminAuthEmailSender
    admin_auth_rate_limiter: AdminAuthRateLimiter
    admin_operations: AdminOperationsRepository
    admin_subscription_adjust_uow_factory: AdminSubscriptionAdjustUnitOfWorkFactory
    billing_auths: BillingAuthRepository
    billing_auth_issue_uow_factory: BillingAuthIssueUnitOfWorkFactory
    catalog_repository: CatalogRepository
    billing_methods: BillingMethodRepository
    billing_method_default_uow_factory: BillingMethodDefaultUnitOfWorkFactory
    billing_method_delete_uow_factory: BillingMethodDeleteUnitOfWorkFactory
    billing_retries: BillingRetryRepository
    invoices: InvoiceRepository
    idempotency_keys: IdempotencyKeyRepository
    operation_locks: OperationLockRepository
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory
    payment_attempts: PaymentAttemptRepository
    payment_customers: PaymentCustomerRepository
    payment_provider: PaymentProvider
    subscription_accounts: SubscriptionAccountRepository
    subscription_billing_uow_factory: SubscriptionBillingUnitOfWorkFactory
    subscription_change_tokens: SubscriptionChangeTokenCodec
    subscription_change_uow_factory: SubscriptionChangeUnitOfWorkFactory
    subscription_checkouts: SubscriptionCheckoutRepository
    subscription_cancel_uow_factory: SubscriptionCancelUnitOfWorkFactory
    subscription_confirm_uow_factory: SubscriptionConfirmUnitOfWorkFactory
    subscription_expirations: SubscriptionExpirationRepository
    subscription_expiration_uow_factory: SubscriptionExpirationUnitOfWorkFactory
    subscription_resume_uow_factory: SubscriptionResumeUnitOfWorkFactory
    webhooks: WebhookRepository
    webhook_uow_factory: WebhookUnitOfWorkFactory
    billing_key_cipher: BillingKeyCipher
    clock: Clock
    internal_service_token: str
    toss_client_key: str = "test_ck_local"
    toss_webhook_secret: str = ""
    allowed_redirect_hosts: tuple[str, ...] = ("example.com",)


def request_context_dependency(
    internal_service_token: str,
    require_user: bool,
):
    async def dependency(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
        x_request_user_id: Annotated[
            str | None, Header(alias="X-Request-User-Id")
        ] = None,
    ) -> RequestContext:
        return build_request_context(
            internal_service_token=internal_service_token,
            require_user=require_user,
            authorization=authorization,
            x_request_id=x_request_id,
            x_request_user_id=x_request_user_id,
        )

    return dependency


def internal_job_context_dependency(internal_service_token: str):
    async def dependency(
        internal_job_token: Annotated[
            str | None, Header(alias="Internal-Job-Token")
        ] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> RequestContext:
        if internal_job_token != internal_service_token:
            raise AuthenticationError("valid internal job token is required")
        if not x_request_id:
            raise AuthorizationError("X-Request-Id header is required")
        return RequestContext(request_id=x_request_id)

    return dependency


def admin_context_dependency(
    repository: AdminAuthRepository,
    clock: Clock,
    access_token_secret: str,
    required_permissions: tuple[str, ...],
):
    async def dependency(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> AdminRequestContext:
        if not x_request_id:
            raise AuthorizationError("X-Request-Id header is required")
        admin = await get_current_admin(
            admin_access_token=_bearer_token(authorization),
            repository=repository,
            clock=clock,
            access_token_secret=access_token_secret,
        )
        if not set(required_permissions).intersection(admin.permissions):
            raise ForbiddenError("admin permission is required")
        return AdminRequestContext(
            request_id=x_request_id,
            admin_id=admin.admin_id,
            request_ip=_client_ip(request),
        )

    return dependency


def build_request_context(
    internal_service_token: str,
    require_user: bool,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    x_request_user_id: Annotated[str | None, Header(alias="X-Request-User-Id")] = None,
) -> RequestContext:
    expected = f"Bearer {internal_service_token}"
    if authorization != expected:
        raise AuthenticationError("valid internal service authorization is required")
    if not x_request_id:
        raise AuthorizationError("X-Request-Id header is required")
    if require_user and not x_request_user_id:
        raise AuthorizationError("X-Request-User-Id header is required")
    return RequestContext(request_id=x_request_id, user_id=x_request_user_id)


def _bearer_token(value: str | None) -> str:
    prefix = "Bearer "
    if value is None or not value.startswith(prefix):
        raise AuthenticationError("admin access token is required")
    return value.removeprefix(prefix)


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host
