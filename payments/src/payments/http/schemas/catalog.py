from __future__ import annotations

from pydantic import BaseModel, Field

from payments.application.catalog import SubscriptionPlanSummary


class EntitlementResponse(BaseModel):
    code: str
    label: str


class PlanListItemResponse(BaseModel):
    plan_id: str = Field(alias="planId")
    plan_code: str = Field(alias="planCode")
    name: str
    billing_period: str = Field(alias="billingPeriod")
    amount: int
    currency: str
    status: str
    is_purchasable: bool = Field(alias="isPurchasable")
    unavailable_reason: str | None = Field(default=None, alias="unavailableReason")
    entitlements: list[str]
    detail_url: str = Field(alias="detailUrl")


class ProductPlanGroupResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_code: str = Field(alias="productCode")
    product_name: str = Field(alias="productName")
    plans: list[PlanListItemResponse]


class PlanResponse(BaseModel):
    product_id: str = Field(alias="productId")
    product_code: str = Field(alias="productCode")
    product_name: str = Field(alias="productName")
    plan_id: str = Field(alias="planId")
    plan_code: str = Field(alias="planCode")
    name: str
    description: str | None
    billing_period: str = Field(alias="billingPeriod")
    amount: int
    currency: str
    status: str
    is_purchasable: bool = Field(alias="isPurchasable")
    unavailable_reason: str | None = Field(alias="unavailableReason")
    entitlements: list[EntitlementResponse]
    checkout_url: str = Field(alias="checkoutUrl")


class PlansResponse(BaseModel):
    items: list[ProductPlanGroupResponse]


def plan_response(summary: SubscriptionPlanSummary) -> PlanResponse:
    return PlanResponse(
        productId=summary.product_id,
        productCode=summary.product_code,
        productName=summary.product_name,
        planId=summary.id,
        planCode=summary.plan_code,
        name=summary.name,
        description=None,
        billingPeriod=summary.billing_period,
        amount=summary.amount,
        currency=summary.currency,
        status=summary.status,
        isPurchasable=summary.is_purchasable,
        unavailableReason=summary.unavailable_reason,
        entitlements=_entitlement_details(summary.entitlements),
        checkoutUrl="/subscriptions/checkout",
    )


def plans_response(summaries: list[SubscriptionPlanSummary]) -> PlansResponse:
    groups: dict[str, ProductPlanGroupResponse] = {}
    for summary in summaries:
        group = groups.get(summary.product_id)
        if group is None:
            group = ProductPlanGroupResponse(
                productId=summary.product_id,
                productCode=summary.product_code,
                productName=summary.product_name,
                plans=[],
            )
            groups[summary.product_id] = group
        group.plans.append(_plan_list_item_response(summary))
    return PlansResponse(items=list(groups.values()))


def _plan_list_item_response(
    summary: SubscriptionPlanSummary,
) -> PlanListItemResponse:
    return PlanListItemResponse(
        planId=summary.id,
        planCode=summary.plan_code,
        name=summary.name,
        billingPeriod=summary.billing_period,
        amount=summary.amount,
        currency=summary.currency,
        status=summary.status,
        isPurchasable=summary.is_purchasable,
        unavailableReason=summary.unavailable_reason,
        entitlements=_entitlement_codes(summary.entitlements),
        detailUrl=f"/plans/{summary.id}",
    )


def _entitlement_codes(entitlements: dict) -> list[str]:
    return [str(key) for key, value in entitlements.items() if value]


def _entitlement_details(entitlements: dict) -> list[EntitlementResponse]:
    return [
        EntitlementResponse(code=code, label=_entitlement_label(code))
        for code in _entitlement_codes(entitlements)
    ]


def _entitlement_label(code: str) -> str:
    return code.replace("_", " ").title()
