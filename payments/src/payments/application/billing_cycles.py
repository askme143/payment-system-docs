from __future__ import annotations

from calendar import monthrange
from datetime import datetime
from typing import Protocol


class BillingAnchorSource(Protocol):
    billing_anchor_day: int | None
    current_period_start_at: datetime | None


def next_billing_at(
    current: datetime,
    billing_period: str,
    billing_anchor_day: int | None = None,
) -> datetime:
    months = 12 if billing_period == "yearly" else 1
    target_year, target_month = _add_months(current.year, current.month, months)
    target_day = min(
        _normalized_anchor_day(billing_anchor_day, current),
        monthrange(target_year, target_month)[1],
    )
    return current.replace(year=target_year, month=target_month, day=target_day)


def billing_anchor_day_for(source: BillingAnchorSource, current: datetime) -> int:
    if source.billing_anchor_day is not None:
        return _normalized_anchor_day(source.billing_anchor_day, current)
    if source.current_period_start_at is not None:
        return source.current_period_start_at.day
    return current.day


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    month_index = month - 1 + months
    return year + month_index // 12, month_index % 12 + 1


def _normalized_anchor_day(anchor_day: int | None, current: datetime) -> int:
    if anchor_day is None:
        return current.day
    return max(1, min(anchor_day, 31))
