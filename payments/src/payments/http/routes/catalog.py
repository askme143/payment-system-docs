from __future__ import annotations

from fastapi import APIRouter, Depends

from payments.application.catalog import get_subscription_plan, list_subscription_plans
from payments.application.context import RequestContext
from payments.application.errors import BadRequestError
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.catalog import (
    PlanResponse,
    PlansResponse,
    plan_response,
    plans_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["plans"])

    require_context = request_context_dependency(
        dependencies.internal_service_token, False
    )

    @router.get(
        "/plans",
        response_model=PlansResponse,
        response_model_exclude_none=True,
    )
    async def list_plans(
        productCode: str | None = None,
        billingPeriod: str | None = None,
        includeUnavailable: str | None = None,
        ctx: RequestContext = Depends(require_context),
    ) -> PlansResponse:
        plans = await list_subscription_plans(
            dependencies.catalog_repository,
            product_code=_optional_query_text(productCode, "productCode"),
            billing_period=_billing_period(billingPeriod),
            include_unavailable=_include_unavailable(includeUnavailable),
            user_id=ctx.user_id,
        )
        return plans_response(plans)

    @router.get("/plans/{planId}", response_model=PlanResponse)
    async def get_plan(
        planId: str,
        ctx: RequestContext = Depends(require_context),
    ) -> PlanResponse:
        plan = await get_subscription_plan(
            planId,
            dependencies.catalog_repository,
            user_id=ctx.user_id,
        )
        return plan_response(plan)

    return router


def _optional_query_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise BadRequestError(f"{field_name} is invalid")
    return normalized


def _billing_period(value: str | None) -> str | None:
    normalized = _optional_query_text(value, "billingPeriod")
    if normalized is None:
        return None
    if normalized not in {"monthly", "yearly"}:
        raise BadRequestError("billingPeriod is invalid")
    return normalized


def _include_unavailable(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise BadRequestError("includeUnavailable is invalid")
