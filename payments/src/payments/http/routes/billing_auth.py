from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, status

from payments.application.billing_auth import (
    BillingAuthIssueCommand,
    BillingAuthStartCommand,
    issue_billing_key,
    start_billing_auth,
)
from payments.application.context import RequestContext
from payments.application.errors import BadRequestError
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.billing_auth import (
    BillingAuthIssueRequest,
    BillingAuthIssueResponse,
    BillingAuthStartRequest,
    BillingAuthStartResponse,
    billing_auth_issue_response,
    billing_auth_start_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/billing", tags=["billing-auth"])
    require_context = request_context_dependency(
        dependencies.internal_service_token,
        True,
    )

    @router.post(
        "/auth",
        response_model=BillingAuthStartResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def start_auth(
        request: BillingAuthStartRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: RequestContext = Depends(require_context),
    ) -> BillingAuthStartResponse:
        result = await start_billing_auth(
            context,
            BillingAuthStartCommand(
                success_url=_required_text(request.success_url, "successUrl"),
                fail_url=_required_text(request.fail_url, "failUrl"),
                set_as_default=_body_bool(
                    request.set_as_default,
                    "setAsDefault",
                ),
            ),
            dependencies.billing_auths,
            dependencies.payment_customers,
            dependencies.idempotency_keys,
            dependencies.clock,
            dependencies.toss_client_key,
            idempotency_key=idempotency_key,
            allowed_redirect_hosts=dependencies.allowed_redirect_hosts,
        )
        return billing_auth_start_response(result)

    @router.post(
        "/issue",
        response_model=BillingAuthIssueResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def issue_auth(
        request: BillingAuthIssueRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: RequestContext = Depends(require_context),
    ) -> BillingAuthIssueResponse:
        result = await issue_billing_key(
            context,
            BillingAuthIssueCommand(
                billing_auth_id=_required_text(
                    request.billing_auth_id,
                    "billingAuthId",
                ),
                auth_key=_required_text(request.auth_key, "authKey"),
                customer_key=_required_text(request.customer_key, "customerKey"),
            ),
            dependencies.billing_auths,
            dependencies.payment_customers,
            dependencies.idempotency_keys,
            dependencies.payment_provider,
            dependencies.clock,
            dependencies.billing_key_cipher,
            idempotency_key=_required_header(idempotency_key, "Idempotency-Key"),
            billing_auth_issue_uow_factory=(
                dependencies.billing_auth_issue_uow_factory
            ),
        )
        return billing_auth_issue_response(result)

    return router


def _required_header(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise BadRequestError(f"{field_name} header is required")
    return value


def _required_text(value: object | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value


def _body_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise BadRequestError(f"{field_name} is invalid")
    return value
