from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header

from payments.application.billing_methods import (
    delete_billing_method,
    get_user_billing_methods,
    set_default_billing_method,
)
from payments.application.context import RequestContext
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.billing_methods import (
    BillingMethodListResponse,
    DeleteBillingMethodResponse,
    SetDefaultBillingMethodResponse,
    billing_method_list_response,
    delete_billing_method_response,
    set_default_billing_method_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["billing-methods"])
    require_user_context = request_context_dependency(
        dependencies.internal_service_token,
        True,
    )

    @router.get("/billing/methods", response_model=BillingMethodListResponse)
    async def list_billing_methods(
        ctx: RequestContext = Depends(require_user_context),
    ) -> BillingMethodListResponse:
        result = await get_user_billing_methods(
            requester=ctx,
            billing_methods=dependencies.billing_methods,
        )
        return billing_method_list_response(result)

    @router.patch(
        "/billing/methods/{billingMethodId}/default",
        response_model=SetDefaultBillingMethodResponse,
    )
    async def set_default_method(
        billingMethodId: str,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> SetDefaultBillingMethodResponse:
        result = await set_default_billing_method(
            requester=ctx,
            billing_method_id=billingMethodId,
            billing_methods=dependencies.billing_methods,
            changed_at=dependencies.clock.utc_now(),
            idempotency_keys=dependencies.idempotency_keys,
            idempotency_key=idempotency_key,
            billing_method_default_uow_factory=(
                dependencies.billing_method_default_uow_factory
            ),
        )
        return set_default_billing_method_response(result)

    @router.delete(
        "/billing/methods/{billingMethodId}",
        response_model=DeleteBillingMethodResponse,
    )
    async def delete_method(
        billingMethodId: str,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> DeleteBillingMethodResponse:
        result = await delete_billing_method(
            requester=ctx,
            billing_method_id=billingMethodId,
            billing_methods=dependencies.billing_methods,
            deleted_at=dependencies.clock.utc_now(),
            idempotency_keys=dependencies.idempotency_keys,
            idempotency_key=idempotency_key,
            billing_method_delete_uow_factory=(
                dependencies.billing_method_delete_uow_factory
            ),
        )
        return delete_billing_method_response(result)

    return router
