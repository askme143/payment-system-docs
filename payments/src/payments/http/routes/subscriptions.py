from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Header

from payments.application.context import RequestContext
from payments.application.errors import BadRequestError
from payments.application.subscription_changes import (
    SubscriptionChangeCommand,
    SubscriptionChangePreviewCommand,
    create_subscription_change_preview,
    execute_subscription_change,
)
from payments.application.subscription_checkout import (
    SubscriptionCheckoutCommand,
    SubscriptionConfirmCommand,
    confirm_subscription_checkout,
    create_subscription_checkout,
)
from payments.application.subscriptions import (
    cancel_subscription_at_period_end,
    get_current_user_subscriptions,
    resume_subscription,
)
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.subscriptions import (
    CancelSubscriptionRequest,
    CurrentUserSubscriptionsResponse,
    ResumeSubscriptionRequest,
    SubscriptionChangePreviewRequest,
    SubscriptionChangePreviewResponse,
    SubscriptionChangeRequest,
    SubscriptionChangeResponse,
    SubscriptionCheckoutRequest,
    SubscriptionCheckoutResponse,
    SubscriptionConfirmRequest,
    SubscriptionConfirmResponse,
    SubscriptionMutationResponse,
    SubscriptionResumeResponse,
    current_user_subscriptions_response,
    subscription_change_preview_response,
    subscription_change_response,
    subscription_checkout_response,
    subscription_confirm_response,
    subscription_mutation_response,
    subscription_resume_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["subscriptions"])
    require_user_context = request_context_dependency(
        dependencies.internal_service_token,
        True,
    )

    @router.get("/subscriptions/me", response_model=CurrentUserSubscriptionsResponse)
    async def get_my_subscriptions(
        ctx: RequestContext = Depends(require_user_context),
    ) -> CurrentUserSubscriptionsResponse:
        account = await get_current_user_subscriptions(
            requester=ctx,
            subscriptions=dependencies.subscription_accounts,
        )
        return current_user_subscriptions_response(account)

    @router.post(
        "/subscriptions/checkout",
        response_model=SubscriptionCheckoutResponse,
        status_code=201,
    )
    async def create_checkout(
        request: SubscriptionCheckoutRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> SubscriptionCheckoutResponse:
        result = await create_subscription_checkout(
            ctx,
            SubscriptionCheckoutCommand(
                plan_id=_required_text(request.plan_id, "planId"),
                success_url=_http_url_text(request.success_url, "successUrl"),
                fail_url=_http_url_text(request.fail_url, "failUrl"),
            ),
            dependencies.catalog_repository,
            dependencies.subscription_checkouts,
            dependencies.payment_customers,
            dependencies.idempotency_keys,
            dependencies.clock,
            dependencies.toss_client_key,
            idempotency_key=idempotency_key,
        )
        return subscription_checkout_response(result)

    @router.post(
        "/subscriptions/confirm",
        response_model=SubscriptionConfirmResponse,
    )
    async def confirm_checkout(
        request: SubscriptionConfirmRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> SubscriptionConfirmResponse:
        result = await confirm_subscription_checkout(
            ctx,
            SubscriptionConfirmCommand(
                subscription_id=_required_text(
                    request.subscription_id,
                    "subscriptionId",
                ),
                customer_key=_required_text(request.customer_key, "customerKey"),
                auth_key=_required_text(request.auth_key, "authKey"),
            ),
            dependencies.catalog_repository,
            dependencies.subscription_checkouts,
            dependencies.billing_auths,
            dependencies.payment_customers,
            dependencies.idempotency_keys,
            dependencies.payment_provider,
            dependencies.clock,
            dependencies.billing_key_cipher,
            dependencies.subscription_confirm_uow_factory,
            idempotency_key=_required_header(idempotency_key, "Idempotency-Key"),
            operation_locks=dependencies.operation_locks,
        )
        return subscription_confirm_response(result)

    @router.post(
        "/subscriptions/{subscriptionId}/change-preview",
        response_model=SubscriptionChangePreviewResponse,
    )
    async def create_change_preview(
        subscriptionId: str,
        request: SubscriptionChangePreviewRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> SubscriptionChangePreviewResponse:
        if request.forbidden_date_fields():
            raise BadRequestError("billing date cannot be changed here")
        result = await create_subscription_change_preview(
            ctx,
            subscriptionId,
            SubscriptionChangePreviewCommand(
                target_plan_id=_required_text(
                    request.target_plan_id,
                    "targetPlanId",
                )
            ),
            dependencies.subscription_accounts,
            dependencies.catalog_repository,
            dependencies.subscription_change_tokens,
            dependencies.clock,
            dependencies.idempotency_keys,
            idempotency_key=idempotency_key,
        )
        return subscription_change_preview_response(result)

    @router.patch(
        "/subscriptions/{subscriptionId}",
        response_model=SubscriptionChangeResponse,
    )
    async def execute_change(
        subscriptionId: str,
        request: SubscriptionChangeRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> SubscriptionChangeResponse:
        if request.forbidden_date_fields():
            raise BadRequestError("billing date cannot be changed here")
        result = await execute_subscription_change(
            ctx,
            subscriptionId,
            SubscriptionChangeCommand(
                confirmation_token=_required_text(
                    request.confirmation_token,
                    "confirmationToken",
                ),
                confirmed=_confirmed_true(request.confirmed),
            ),
            dependencies.subscription_accounts,
            dependencies.catalog_repository,
            dependencies.subscription_change_tokens,
            dependencies.billing_retries,
            dependencies.payment_customers,
            dependencies.idempotency_keys,
            dependencies.payment_provider,
            dependencies.clock,
            dependencies.billing_key_cipher,
            idempotency_key=_required_header(idempotency_key, "Idempotency-Key"),
            operation_locks=dependencies.operation_locks,
            subscription_change_uow_factory=(
                dependencies.subscription_change_uow_factory
            ),
        )
        return subscription_change_response(result)

    @router.post(
        "/subscriptions/{subscriptionId}/cancel",
        response_model=SubscriptionMutationResponse,
    )
    async def cancel_subscription(
        subscriptionId: str,
        request: Annotated[CancelSubscriptionRequest | None, Body()] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> SubscriptionMutationResponse:
        body = request or CancelSubscriptionRequest()
        result = await cancel_subscription_at_period_end(
            requester=ctx,
            subscription_id=subscriptionId,
            subscriptions=dependencies.subscription_accounts,
            canceled_at=dependencies.clock.utc_now(),
            idempotency_keys=dependencies.idempotency_keys,
            idempotency_key=idempotency_key,
            cancel_reason=body.cancel_reason,
            feedback=body.feedback,
            operation_locks=dependencies.operation_locks,
            subscription_cancel_uow_factory=(
                dependencies.subscription_cancel_uow_factory
            ),
        )
        return subscription_mutation_response(result)

    @router.post(
        "/subscriptions/{subscriptionId}/resume",
        response_model=SubscriptionResumeResponse,
    )
    async def resume_cancel_scheduled_subscription(
        subscriptionId: str,
        request: Annotated[ResumeSubscriptionRequest | None, Body()] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> SubscriptionResumeResponse:
        body = request or ResumeSubscriptionRequest()
        result = await resume_subscription(
            requester=ctx,
            subscription_id=subscriptionId,
            subscriptions=dependencies.subscription_accounts,
            now=dependencies.clock.utc_now(),
            idempotency_keys=dependencies.idempotency_keys,
            idempotency_key=idempotency_key,
            resume_reason=body.resume_reason,
            operation_locks=dependencies.operation_locks,
            subscription_resume_uow_factory=(
                dependencies.subscription_resume_uow_factory
            ),
        )
        return subscription_resume_response(result)

    return router


def _required_header(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise BadRequestError(f"{field_name} header is required")
    return value


def _required_text(value: object | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value


def _http_url_text(value: object | None, field_name: str) -> str:
    text = _required_text(value, field_name)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BadRequestError(f"{field_name} is invalid")
    return text


def _confirmed_true(value: object | None) -> bool:
    if value is not True:
        raise BadRequestError("confirmed must be true")
    return True
