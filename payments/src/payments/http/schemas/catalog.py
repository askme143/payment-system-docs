from __future__ import annotations

from pydantic import BaseModel, Field

from payments.application.catalog import SubscriptionPlanSummary


class PlanResponse(BaseModel):
    id: str
    product_id: str = Field(alias="productId")
    product_code: str = Field(alias="productCode")
    name: str
    plan_code: str = Field(alias="planCode")
    billing_period: str = Field(alias="billingPeriod")
    amount: int
    entitlements: dict
    status: str


class PlansResponse(BaseModel):
    plans: list[PlanResponse]


def plan_response(summary: SubscriptionPlanSummary) -> PlanResponse:
    return PlanResponse(
        id=summary.id,
        productId=summary.product_id,
        productCode=summary.product_code,
        name=summary.name,
        planCode=summary.plan_code,
        billingPeriod=summary.billing_period,
        amount=summary.amount,
        entitlements=summary.entitlements,
        status=summary.status,
    )
