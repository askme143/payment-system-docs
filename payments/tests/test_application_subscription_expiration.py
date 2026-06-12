from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from payments.application.jobs.subscription_expiration import (
    expire_cancel_scheduled_subscriptions,
)
from payments.domain.entities.subscription import Subscription


def subscription(
    subscription_id: str,
    status: Literal["pending", "active", "past_due", "cancel_scheduled", "canceled"],
    current_period_end_at: datetime | None,
) -> Subscription:
    return Subscription(
        id=subscription_id,
        user_id="user_1",
        payment_customer_id="cust_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status=status,
        cancel_at_period_end=status == "cancel_scheduled",
        next_billing_at=current_period_end_at,
        current_period_start_at=datetime(2026, 5, 10, tzinfo=UTC),
        current_period_end_at=current_period_end_at,
    )


async def test_expire_cancel_scheduled_subscriptions_marks_only_elapsed_targets(
    test_dependencies,
) -> None:
    now = test_dependencies.clock.utc_now()
    expired = subscription("sub_expired", "cancel_scheduled", now)
    future = subscription(
        "sub_future",
        "cancel_scheduled",
        now + timedelta(seconds=1),
    )
    active = subscription("sub_active", "active", now)
    already_canceled = subscription("sub_canceled", "canceled", now)
    test_dependencies.subscription_expirations.subscriptions = {
        item.id: item
        for item in [expired, future, active, already_canceled]
    }

    summary = await expire_cancel_scheduled_subscriptions(
        subscriptions=test_dependencies.subscription_expirations,
        clock=test_dependencies.clock,
        limit=100,
        operation_locks=test_dependencies.operation_locks,
        subscription_expiration_uow_factory=(
            test_dependencies.subscription_expiration_uow_factory
        ),
    )

    assert summary.selected_count == 1
    assert summary.processed_count == 1
    assert summary.skipped_count == 0
    assert summary.failed_count == 0
    assert summary.cancel_expiration_emails_queued == 1
    assert summary.expired_subscription_ids == ["sub_expired"]
    assert expired.status == "canceled"
    assert expired.canceled_at == now
    assert expired.access_until == now
    assert expired.next_billing_at is None
    assert future.status == "cancel_scheduled"
    assert active.status == "active"
    assert already_canceled.status == "canceled"
    assert test_dependencies.operation_locks.acquire_calls == [
        "internal-billing-run:cancel_expiration:2026-06-10"
    ]
    assert test_dependencies.operation_locks.release_calls == [
        "internal-billing-run:cancel_expiration:2026-06-10"
    ]
    assert test_dependencies.subscription_expiration_uow_factory.enter_count == 1
    assert test_dependencies.subscription_expiration_uow_factory.commit_count == 1
    audit = next(
        iter(test_dependencies.payment_stores.operator_audits.operator_audits.values())
    )
    assert audit.action == "subscription.cancel_expired"
    assert audit.operator_id == "system:subscription-expiration"
    assert audit.target_id == "sub_expired"
    assert audit.previous_state["status"] == "cancel_scheduled"
    assert audit.next_state["status"] == "canceled"
    assert audit.next_state["notification"] == {
        "template": "subscription_canceled_after_period",
        "queued": True,
    }


async def test_expire_cancel_scheduled_subscriptions_is_bounded_by_limit(
    test_dependencies,
) -> None:
    now = test_dependencies.clock.utc_now()
    test_dependencies.subscription_expirations.subscriptions = {
        f"sub_{index}": subscription(f"sub_{index}", "cancel_scheduled", now)
        for index in range(3)
    }

    summary = await expire_cancel_scheduled_subscriptions(
        subscriptions=test_dependencies.subscription_expirations,
        clock=test_dependencies.clock,
        limit=2,
    )

    assert summary.selected_count == 2
    assert summary.processed_count == 2
    assert summary.cancel_expiration_emails_queued == 2
    assert len(summary.expired_subscription_ids) == 2


async def test_expire_cancel_scheduled_subscriptions_honors_cancel_at(
    test_dependencies,
) -> None:
    now = test_dependencies.clock.utc_now()
    expired_by_cancel_at = subscription(
        "sub_expired_by_cancel_at",
        "cancel_scheduled",
        now + timedelta(days=1),
    )
    expired_by_cancel_at.cancel_at = now
    test_dependencies.subscription_expirations.subscriptions = {
        expired_by_cancel_at.id: expired_by_cancel_at
    }

    summary = await expire_cancel_scheduled_subscriptions(
        subscriptions=test_dependencies.subscription_expirations,
        clock=test_dependencies.clock,
        limit=100,
    )

    assert summary.processed_count == 1
    assert expired_by_cancel_at.status == "canceled"
