from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from payments.application.admin_catalog import (
    AdminOneTimeSkuCreateCommand,
    AdminOneTimeSkuUpdateCommand,
    AdminProductCreateCommand,
    AdminProductQuery,
    AdminProductStatusChangeCommand,
    AdminRequestContext,
    AdminSubscriptionPlanCreateCommand,
    AdminSubscriptionPlanUpdateCommand,
    change_admin_product_status,
    create_admin_one_time_sku,
    create_admin_product,
    create_admin_subscription_plan,
    get_admin_product_detail,
    list_admin_products,
    update_admin_one_time_sku,
    update_admin_subscription_plan,
)
from payments.application.errors import BadRequestError
from payments.http.dependencies import HttpDependencies, admin_context_dependency
from payments.http.schemas.admin_catalog import (
    AdminOneTimeSkuCreateRequest,
    AdminOneTimeSkuResponse,
    AdminOneTimeSkuUpdateRequest,
    AdminOneTimeSkuUpdateResponse,
    AdminProductCreateRequest,
    AdminProductDetailResponse,
    AdminProductListResponse,
    AdminProductResponse,
    AdminProductStatusChangeRequest,
    AdminProductStatusChangeResponse,
    AdminSubscriptionPlanCreateRequest,
    AdminSubscriptionPlanResponse,
    AdminSubscriptionPlanUpdateRequest,
    AdminSubscriptionPlanUpdateResponse,
    admin_one_time_sku_response,
    admin_one_time_sku_update_response,
    admin_product_detail_response,
    admin_product_list_response,
    admin_product_response,
    admin_product_status_change_response,
    admin_subscription_plan_response,
    admin_subscription_plan_update_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin-catalog"])
    require_admin_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("product_manage",),
    )

    @router.get("/products", response_model=AdminProductListResponse)
    async def list_products(
        context: AdminRequestContext = Depends(require_admin_context),
        productType: str | None = None,
        status: Annotated[list[str] | None, Query()] = None,
        keyword: str | None = None,
        cursor: str | None = None,
        limit: str = "50",
    ) -> AdminProductListResponse:
        _ = context
        result = await list_admin_products(
            AdminProductQuery(
                product_type=productType,
                status=tuple(status) if status is not None else None,
                keyword=keyword,
                cursor=cursor,
                limit=_admin_catalog_limit(limit),
            ),
            dependencies.admin_catalog,
        )
        return admin_product_list_response(result)

    @router.get(
        "/products/{productId}",
        response_model=AdminProductDetailResponse,
    )
    async def get_product_detail(
        productId: str,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> AdminProductDetailResponse:
        _ = context
        result = await get_admin_product_detail(productId, dependencies.admin_catalog)
        return admin_product_detail_response(result)

    @router.post(
        "/products",
        response_model=AdminProductResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_product(
        request: AdminProductCreateRequest,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> AdminProductResponse:
        product = await create_admin_product(
            AdminProductCreateCommand(
                product_code=request.product_code,
                product_type=request.product_type,
                name=request.name,
                status=request.status,
            ),
            context,
            dependencies.admin_catalog,
        )
        return admin_product_response(product)

    @router.patch(
        "/products/{productId}/status",
        response_model=AdminProductStatusChangeResponse,
    )
    async def change_product_status(
        productId: str,
        request: AdminProductStatusChangeRequest,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> AdminProductStatusChangeResponse:
        result = await change_admin_product_status(
            productId,
            AdminProductStatusChangeCommand(
                status=request.status,
                reason=request.reason,
                effective_at=request.effective_at,
            ),
            context,
            dependencies.admin_catalog,
            dependencies.clock,
        )
        return admin_product_status_change_response(result)

    @router.post(
        "/products/{productId}/subscription-plans",
        response_model=AdminSubscriptionPlanResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_subscription_plan(
        productId: str,
        request: AdminSubscriptionPlanCreateRequest,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> AdminSubscriptionPlanResponse:
        plan = await create_admin_subscription_plan(
            productId,
            AdminSubscriptionPlanCreateCommand(
                plan_code=request.plan_code,
                billing_period=request.billing_period,
                amount=request.amount,
                currency=request.currency,
                status=request.status,
                entitlements=request.entitlements,
            ),
            context,
            dependencies.admin_catalog,
        )
        return admin_subscription_plan_response(plan, "subscription")

    @router.patch(
        "/products/{productId}/subscription-plans/{planId}",
        response_model=AdminSubscriptionPlanUpdateResponse,
    )
    async def update_subscription_plan(
        productId: str,
        planId: str,
        request: AdminSubscriptionPlanUpdateRequest,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> AdminSubscriptionPlanUpdateResponse:
        result = await update_admin_subscription_plan(
            productId,
            planId,
            AdminSubscriptionPlanUpdateCommand(
                amount=request.amount,
                currency=request.currency,
                status=request.status,
                entitlements=request.entitlements,
                change_reason=request.change_reason,
            ),
            context,
            dependencies.admin_catalog,
        )
        return admin_subscription_plan_update_response(result)

    @router.post(
        "/products/{productId}/one-time-skus",
        response_model=AdminOneTimeSkuResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_one_time_sku(
        productId: str,
        request: AdminOneTimeSkuCreateRequest,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> AdminOneTimeSkuResponse:
        sku = await create_admin_one_time_sku(
            productId,
            AdminOneTimeSkuCreateCommand(
                sku_code=request.sku_code,
                amount=request.amount,
                currency=request.currency,
                status=request.status,
                stock_policy=request.stock_policy,
                total_stock=request.total_stock,
                purchase_limit=request.purchase_limit,
            ),
            context,
            dependencies.admin_catalog,
        )
        return admin_one_time_sku_response(sku, "one_time")

    @router.patch(
        "/products/{productId}/one-time-skus/{skuId}",
        response_model=AdminOneTimeSkuUpdateResponse,
    )
    async def update_one_time_sku(
        productId: str,
        skuId: str,
        request: AdminOneTimeSkuUpdateRequest,
        context: AdminRequestContext = Depends(require_admin_context),
    ) -> AdminOneTimeSkuUpdateResponse:
        result = await update_admin_one_time_sku(
            productId,
            skuId,
            AdminOneTimeSkuUpdateCommand(
                amount=request.amount,
                currency=request.currency,
                status=request.status,
                stock_policy=request.stock_policy,
                total_stock=request.total_stock,
                purchase_limit=request.purchase_limit,
                change_reason=request.change_reason,
            ),
            context,
            dependencies.admin_catalog,
        )
        return admin_one_time_sku_update_response(result)

    return router


def _admin_catalog_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise BadRequestError("limit is invalid") from exc
    if limit < 1 or limit > 100:
        raise BadRequestError("limit is invalid")
    return limit
