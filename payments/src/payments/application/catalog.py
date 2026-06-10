from __future__ import annotations

from dataclasses import dataclass

from payments.application.errors import ResourceNotFoundError
from payments.application.ports import CatalogRepository


@dataclass(frozen=True, slots=True)
class SubscriptionPlanSummary:
    id: str
    product_id: str
    product_code: str
    name: str
    plan_code: str
    billing_period: str
    amount: int
    entitlements: dict
    status: str


async def list_subscription_plans(
    catalog_repository: CatalogRepository,
) -> list[SubscriptionPlanSummary]:
    """판매 가능한 구독 플랜 목록을 조회합니다.

    Args:
        catalog_repository: 활성 상품과 활성 구독 플랜을 조회하는 저장소입니다.

    Returns:
        판매 가능한 구독 플랜 요약 목록입니다.

    Raises:
        ResourceNotFoundError: 목록 조회에서는 발생하지 않지만 단일 조회와 같은
            오류 계층을 공유합니다.
    """
    rows = await catalog_repository.list_active_subscription_catalog()
    return [_summary_from_row(product, plan) for product, plan in rows]


async def get_subscription_plan(
    plan_id: str,
    catalog_repository: CatalogRepository,
) -> SubscriptionPlanSummary:
    """단일 구독 플랜을 조회합니다.

    Args:
        plan_id: 조회할 구독 플랜 ID입니다.
        catalog_repository: 활성 상품과 활성 구독 플랜을 조회하는 저장소입니다.

    Returns:
        판매 가능한 단일 구독 플랜 요약입니다.

    Raises:
        ResourceNotFoundError: 활성 플랜이나 연결된 활성 상품이 없을 때 발생합니다.
    """
    row = await catalog_repository.get_active_subscription_plan(plan_id)
    if row is None:
        raise ResourceNotFoundError("plan not found")
    return _summary_from_row(*row)


def _summary_from_row(product, plan) -> SubscriptionPlanSummary:
    return SubscriptionPlanSummary(
        id=plan.id,
        product_id=product.id,
        product_code=product.product_code,
        name=product.name,
        plan_code=plan.plan_code,
        billing_period=plan.billing_period,
        amount=plan.amount,
        entitlements=plan.entitlements,
        status=plan.status,
    )
