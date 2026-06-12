from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from payments.application.admin_catalog import (
    AdminOneTimeSkuUpdateResult,
    AdminProductStatusChangeResult,
    AdminSubscriptionPlanUpdateResult,
)
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan


class AdminProductCreateRequest(BaseModel):
    product_code: str = Field(alias="productCode")
    product_type: Literal["subscription", "one_time"] = Field(alias="productType")
    name: str
    status: Literal["draft", "active", "paused", "archived"] = "draft"


class AdminProductResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_code: str = Field(alias="productCode")
    product_type: str = Field(alias="productType")
    status: str
    subscription_plans: list[object] = Field(alias="subscriptionPlans")
    one_time_skus: list[object] = Field(alias="oneTimeSkus")


class AdminProductStatusChangeRequest(BaseModel):
    status: object | None = None
    reason: object | None = None
    effective_at: object | None = Field(default=None, alias="effectiveAt")


class AdminProductStatusChangeResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_code: str = Field(alias="productCode")
    product_type: str = Field(alias="productType")
    previous_status: str = Field(alias="previousStatus")
    status: str
    effective_at: datetime = Field(alias="effectiveAt")


class AdminSubscriptionPlanCreateRequest(BaseModel):
    plan_code: str = Field(alias="planCode")
    plan_name: str | None = Field(default=None, alias="planName")
    billing_period: str = Field(alias="billingPeriod")
    amount: object
    currency: str = "KRW"
    status: str = "draft"
    entitlements: object | None = Field(default_factory=dict)


class AdminSubscriptionPlanResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_type: str = Field(alias="productType")
    plan_id: str = Field(alias="planId")
    status: str
    billing_period: str = Field(alias="billingPeriod")
    amount: int
    currency: str


class AdminSubscriptionPlanUpdateRequest(BaseModel):
    plan_name: str | None = Field(default=None, alias="planName")
    amount: object | None = None
    currency: str | None = None
    status: str | None = None
    display_order: int | None = Field(default=None, alias="displayOrder")
    entitlements: object | None = None
    change_reason: str = Field(alias="changeReason")


class AdminSubscriptionPlanUpdateResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_type: str = Field(alias="productType")
    plan_id: str = Field(alias="planId")
    status: str
    amount: int
    currency: str
    version: int
    effective_for: str = Field(alias="effectiveFor")


class StockPolicyRequest(BaseModel):
    type: Literal["unlimited", "limited"]


class StockPolicyResponse(BaseModel):
    type: Literal["unlimited", "limited"]


class AdminOneTimeSkuCreateRequest(BaseModel):
    sku_code: str = Field(alias="skuCode")
    sku_name: str | None = Field(default=None, alias="skuName")
    amount: object
    currency: str = "KRW"
    status: str = "draft"
    stock_policy: object | None = Field(
        default=None,
        alias="stockPolicy",
    )
    total_stock: object | None = Field(default=None, alias="totalStock")
    purchase_limit: object | None = Field(default=None, alias="purchaseLimit")


class AdminOneTimeSkuResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_type: str = Field(alias="productType")
    sku_id: str = Field(alias="skuId")
    status: str
    amount: int
    currency: str
    stock_policy: StockPolicyResponse = Field(alias="stockPolicy")


class AdminOneTimeSkuUpdateRequest(BaseModel):
    sku_name: str | None = Field(default=None, alias="skuName")
    amount: object | None = None
    currency: str | None = None
    status: str | None = None
    stock_policy: object | None = Field(default=None, alias="stockPolicy")
    total_stock: object | None = Field(default=None, alias="totalStock")
    purchase_limit: object | None = Field(default=None, alias="purchaseLimit")
    change_reason: str = Field(alias="changeReason")


class AdminOneTimeSkuUpdateResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_type: str = Field(alias="productType")
    sku_id: str = Field(alias="skuId")
    status: str
    amount: int
    currency: str
    stock_policy: StockPolicyResponse = Field(alias="stockPolicy")
    total_stock: int | None = Field(alias="totalStock")
    reserved_stock: int | None = Field(alias="reservedStock")
    sold_stock: int | None = Field(alias="soldStock")
    available_stock: int | None = Field(alias="availableStock")
    effective_for: str = Field(alias="effectiveFor")


def admin_product_response(product: Product) -> AdminProductResponse:
    return AdminProductResponse(
        productId=product.id,
        productCode=product.product_code,
        productType=product.product_type,
        status=product.status,
        subscriptionPlans=[],
        oneTimeSkus=[],
    )


def admin_product_status_change_response(
    result: AdminProductStatusChangeResult,
) -> AdminProductStatusChangeResponse:
    product = result.product
    return AdminProductStatusChangeResponse(
        productId=product.id,
        productCode=product.product_code,
        productType=product.product_type,
        previousStatus=result.previous_status,
        status=product.status,
        effectiveAt=result.effective_at,
    )


def admin_subscription_plan_response(
    plan: SubscriptionPlan,
    product_type: str,
) -> AdminSubscriptionPlanResponse:
    return AdminSubscriptionPlanResponse(
        productId=plan.product_id,
        productType=product_type,
        planId=plan.id,
        status=plan.status,
        billingPeriod=plan.billing_period,
        amount=plan.amount,
        currency=plan.currency,
    )


def admin_subscription_plan_update_response(
    result: AdminSubscriptionPlanUpdateResult,
) -> AdminSubscriptionPlanUpdateResponse:
    return AdminSubscriptionPlanUpdateResponse(
        productId=result.plan.product_id,
        productType=result.product_type,
        planId=result.plan.id,
        status=result.plan.status,
        amount=result.plan.amount,
        currency=result.plan.currency,
        version=result.plan.version,
        effectiveFor=result.effective_for,
    )


def admin_one_time_sku_response(
    sku: OneTimeSku,
    product_type: str,
) -> AdminOneTimeSkuResponse:
    return AdminOneTimeSkuResponse(
        productId=sku.product_id,
        productType=product_type,
        skuId=sku.id,
        status=sku.status,
        amount=sku.amount,
        currency=sku.currency,
        stockPolicy=StockPolicyResponse(type=sku.stock_policy),
    )


def admin_one_time_sku_update_response(
    result: AdminOneTimeSkuUpdateResult,
) -> AdminOneTimeSkuUpdateResponse:
    sku = result.sku
    return AdminOneTimeSkuUpdateResponse(
        productId=sku.product_id,
        productType=result.product_type,
        skuId=sku.id,
        status=sku.status,
        amount=sku.amount,
        currency=sku.currency,
        stockPolicy=StockPolicyResponse(type=sku.stock_policy),
        totalStock=sku.total_stock,
        reservedStock=sku.reserved_stock,
        soldStock=sku.sold_stock,
        availableStock=sku.available_stock,
        effectiveFor=result.effective_for,
    )
