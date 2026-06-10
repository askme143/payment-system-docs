from __future__ import annotations

from fastapi import APIRouter, Depends

from payments.application.catalog import get_subscription_plan, list_subscription_plans
from payments.application.context import RequestContext
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.catalog import PlanResponse, PlansResponse, plan_response


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["plans"])

    require_context = request_context_dependency(
        dependencies.internal_service_token, False
    )

    @router.get("/plans", response_model=PlansResponse)
    async def list_plans(
        _ctx: RequestContext = Depends(require_context),
    ) -> PlansResponse:
        plans = await list_subscription_plans(dependencies.catalog_repository)
        return PlansResponse(plans=[plan_response(plan) for plan in plans])

    @router.get("/plans/{planId}", response_model=PlanResponse)
    async def get_plan(
        planId: str,
        _ctx: RequestContext = Depends(require_context),
    ) -> PlanResponse:
        plan = await get_subscription_plan(planId, dependencies.catalog_repository)
        return plan_response(plan)

    return router
