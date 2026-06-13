from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header

from payments.application.errors import BadRequestError
from payments.application.jobs.billing_retry import (
    BillingRetryCommand,
    retry_subscription_billing,
)
from payments.application.jobs.subscription_billing import (
    SubscriptionBillingRunCommand,
)
from payments.application.jobs.subscription_billing import (
    run_subscription_billing as run_recurring_subscription_billing,
)
from payments.application.jobs.subscription_expiration import (
    expire_cancel_scheduled_subscriptions,
)
from payments.http.dependencies import (
    HttpDependencies,
    internal_job_context_dependency,
)
from payments.http.schemas.internal import (
    InternalBillingRetryRequest,
    InternalBillingRetryResponse,
    InternalBillingRunRequest,
    InternalBillingRunResponse,
    billing_retry_response,
    subscription_billing_response,
    subscription_expiration_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["internal"])
    require_job_context = internal_job_context_dependency(
        dependencies.internal_service_token
    )

    @router.post(
        "/internal/subscription-billing/run",
        response_model=InternalBillingRunResponse,
        response_model_exclude_none=True,
    )
    async def run_subscription_billing(
        body: Annotated[InternalBillingRunRequest | None, Body()] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        _=Depends(require_job_context),
    ) -> InternalBillingRunResponse:
        request = body or InternalBillingRunRequest()
        if request.job_type in {"billing", "reminder"}:
            job_type = "reminder" if request.job_type == "reminder" else "billing"
            result = await run_recurring_subscription_billing(
                SubscriptionBillingRunCommand(
                    job_type=job_type,
                    billing_date=request.billing_date,
                    limit=request.limit,
                    dry_run=request.dry_run,
                ),
                dependencies.billing_retries,
                dependencies.payment_customers,
                dependencies.idempotency_keys,
                dependencies.payment_provider,
                dependencies.clock,
                dependencies.billing_key_cipher,
                idempotency_key=idempotency_key,
                operation_locks=dependencies.operation_locks,
                subscription_billing_uow_factory=(
                    dependencies.subscription_billing_uow_factory
                ),
                notification_dependencies=dependencies.notification_enqueue,
            )
            return subscription_billing_response(result)
        summary = await expire_cancel_scheduled_subscriptions(
            subscriptions=dependencies.subscription_expirations,
            clock=dependencies.clock,
            limit=request.limit,
            dry_run=request.dry_run,
            operation_locks=dependencies.operation_locks,
            subscription_expiration_uow_factory=(
                dependencies.subscription_expiration_uow_factory
            ),
            notification_dependencies=dependencies.notification_enqueue,
        )
        return subscription_expiration_response(summary)

    @router.post(
        "/internal/subscription-billing/{invoiceId}/retry",
        response_model=InternalBillingRetryResponse,
    )
    async def retry_subscription_payment(
        invoiceId: str,
        body: Annotated[InternalBillingRetryRequest | None, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _=Depends(require_job_context),
    ) -> InternalBillingRetryResponse:
        request = body or InternalBillingRetryRequest()
        result = await retry_subscription_billing(
            invoiceId,
            BillingRetryCommand(
                force=request.force,
                reason=request.reason,
                dry_run=request.dry_run,
            ),
            dependencies.billing_retries,
            dependencies.payment_customers,
            dependencies.idempotency_keys,
            dependencies.payment_provider,
            dependencies.clock,
            dependencies.billing_key_cipher,
            idempotency_key=_required_header(idempotency_key, "Idempotency-Key"),
            operation_locks=dependencies.operation_locks,
            subscription_billing_uow_factory=(
                dependencies.subscription_billing_uow_factory
            ),
            notification_dependencies=dependencies.notification_enqueue,
        )
        return billing_retry_response(result)

    return router


def _required_header(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise BadRequestError(f"{field_name} header is required")
    return value
