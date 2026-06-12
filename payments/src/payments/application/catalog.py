from __future__ import annotations

from dataclasses import dataclass

from payments.application.errors import ConflictResponseError, ResourceNotFoundError
from payments.application.ports import CatalogRepository
from payments.domain.entities.subscription import Subscription


@dataclass(frozen=True, slots=True)
class SubscriptionPlanSummary:
    id: str
    product_id: str
    product_code: str
    product_name: str
    name: str
    plan_code: str
    billing_period: str
    amount: int
    currency: str
    entitlements: dict
    status: str
    is_purchasable: bool = True
    unavailable_reason: str | None = None


async def list_subscription_plans(
    catalog_repository: CatalogRepository,
    *,
    product_code: str | None = None,
    billing_period: str | None = None,
    include_unavailable: bool = False,
    user_id: str | None = None,
) -> list[SubscriptionPlanSummary]:
    """판매 가능한 구독 플랜 목록을 조회합니다.

    Args:
        catalog_repository: 활성 상품과 활성 구독 플랜을 조회하는 저장소입니다.
        user_id: 로그인 사용자 기준 표시 보정이 필요할 때 전달되는 회원 ID입니다.

    Returns:
        판매 가능한 구독 플랜 요약 목록입니다.

    Raises:
        ResourceNotFoundError: 목록 조회에서는 발생하지 않지만 단일 조회와 같은
            오류 계층을 공유합니다.
    """
    rows = await catalog_repository.list_active_subscription_catalog()
    summaries = [_summary_from_row(product, plan) for product, plan in rows]
    summaries = await _apply_user_subscription_state(
        catalog_repository,
        summaries,
        user_id,
    )
    if not include_unavailable:
        summaries = [summary for summary in summaries if summary.is_purchasable]
    if product_code is not None:
        summaries = [
            summary
            for summary in summaries
            if summary.product_code == product_code
        ]
    if billing_period is not None:
        summaries = [
            summary
            for summary in summaries
            if summary.billing_period == billing_period
        ]
    return _sort_plan_summaries(summaries)


async def get_subscription_plan(
    plan_id: str,
    catalog_repository: CatalogRepository,
    *,
    user_id: str | None = None,
) -> SubscriptionPlanSummary:
    """단일 구독 플랜을 조회합니다.

    Args:
        plan_id: 조회할 구독 플랜 ID입니다.
        catalog_repository: 활성 상품과 활성 구독 플랜을 조회하는 저장소입니다.
        user_id: 로그인 사용자 기준 표시 보정이 필요할 때 전달되는 회원 ID입니다.

    Returns:
        판매 가능한 단일 구독 플랜 요약입니다.

    Raises:
        ResourceNotFoundError: 활성 플랜이나 연결된 활성 상품이 없을 때 발생합니다.
    """
    row = await catalog_repository.get_active_subscription_plan(plan_id)
    if row is None:
        raise ResourceNotFoundError("plan not found")
    summaries = await _apply_user_subscription_state(
        catalog_repository,
        [_summary_from_row(*row)],
        user_id,
    )
    summary = summaries[0]
    if not summary.is_purchasable:
        raise ConflictResponseError(
            "plan is not purchasable",
            {
                "planId": summary.id,
                "isPurchasable": False,
                "unavailableReason": summary.unavailable_reason,
            },
        )
    return summary


async def _apply_user_subscription_state(
    catalog_repository: CatalogRepository,
    summaries: list[SubscriptionPlanSummary],
    user_id: str | None,
) -> list[SubscriptionPlanSummary]:
    if user_id is None:
        return summaries
    subscriptions = await catalog_repository.list_user_active_product_subscriptions(
        user_id
    )
    if not subscriptions:
        return summaries
    by_product_code = {
        subscription.product_code: subscription for subscription in subscriptions
    }
    return [
        _summary_with_subscription_state(
            summary,
            by_product_code.get(summary.product_code),
        )
        for summary in summaries
    ]


def _summary_with_subscription_state(
    summary: SubscriptionPlanSummary,
    subscription: Subscription | None,
) -> SubscriptionPlanSummary:
    if subscription is None:
        return summary
    return SubscriptionPlanSummary(
        id=summary.id,
        product_id=summary.product_id,
        product_code=summary.product_code,
        product_name=summary.product_name,
        name=summary.name,
        plan_code=summary.plan_code,
        billing_period=summary.billing_period,
        amount=summary.amount,
        currency=summary.currency,
        entitlements=summary.entitlements,
        status=summary.status,
        is_purchasable=False,
        unavailable_reason="PRODUCT_ALREADY_SUBSCRIBED",
    )


def _sort_plan_summaries(
    summaries: list[SubscriptionPlanSummary],
) -> list[SubscriptionPlanSummary]:
    return sorted(
        summaries,
        key=lambda summary: (
            summary.product_name.casefold(),
            summary.product_code.casefold(),
            _billing_period_rank(summary.billing_period),
            summary.amount,
            summary.plan_code.casefold(),
        ),
    )


def _billing_period_rank(billing_period: str) -> int:
    return 0 if billing_period == "monthly" else 1


def _summary_from_row(product, plan) -> SubscriptionPlanSummary:
    return SubscriptionPlanSummary(
        id=plan.id,
        product_id=product.id,
        product_code=product.product_code,
        product_name=product.name,
        name=_plan_display_name(plan.plan_code, plan.billing_period),
        plan_code=plan.plan_code,
        billing_period=plan.billing_period,
        amount=plan.amount,
        currency=plan.currency,
        entitlements=plan.entitlements,
        status=plan.status,
    )


def _plan_display_name(plan_code: str, billing_period: str) -> str:
    period_suffixes = {"monthly": "월간", "yearly": "연간"}
    period_label = period_suffixes.get(billing_period, billing_period)
    suffix = f"_{billing_period}"
    base_code = (
        plan_code[: -len(suffix)]
        if suffix and plan_code.endswith(suffix)
        else plan_code
    )
    base_label = " ".join(
        part.capitalize() for part in base_code.split("_") if part
    )
    return f"{base_label} {period_label}".strip()
