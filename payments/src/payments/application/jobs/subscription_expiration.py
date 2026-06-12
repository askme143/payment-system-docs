from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from payments.application.operation_locks import (
    acquire_required_operation_lock,
    release_operation_lock,
)
from payments.application.ports.clock import Clock
from payments.application.ports.operation_locks import OperationLockRepository
from payments.application.ports.operator_audits import OperatorAuditRepository
from payments.application.ports.subscriptions import SubscriptionExpirationRepository
from payments.application.ports.unit_of_work import (
    SubscriptionExpirationUnitOfWorkFactory,
)
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.subscription import Subscription

SUBSCRIPTION_CANCEL_EXPIRATION_JOB_SCOPE = "internal-billing-run:cancel_expiration"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubscriptionExpirationRunSummary:
    selected_count: int
    processed_count: int
    skipped_count: int
    failed_count: int
    cancel_expiration_emails_queued: int
    expired_subscription_ids: list[str]


async def expire_cancel_scheduled_subscriptions(
    *,
    subscriptions: SubscriptionExpirationRepository,
    clock: Clock,
    limit: int,
    dry_run: bool = False,
    operation_locks: OperationLockRepository | None = None,
    subscription_expiration_uow_factory: (
        SubscriptionExpirationUnitOfWorkFactory | None
    ) = None,
    operator_audits: OperatorAuditRepository | None = None,
) -> SubscriptionExpirationRunSummary:
    """해지 예약 기간이 지난 구독을 최종 종료 상태로 전환합니다.

    Args:
        subscriptions: 해지 예약 만료 대상 구독 저장소입니다.
        clock: 배치 기준 시각을 제공하는 시계 포트입니다.
        limit: 한 번에 조회하고 처리할 최대 구독 수입니다.
        dry_run: true이면 대상만 조회하고 상태를 변경하지 않습니다.
        operation_locks: 중복 만료 배치를 막는 작업 잠금 저장소입니다.
        subscription_expiration_uow_factory: 상태 전이와 감사 로그를 묶는 UoW입니다.
        operator_audits: UoW가 없을 때 사용하는 감사 로그 저장소입니다.

    Returns:
        처리 대상 수와 최종 종료된 구독 ID 목록을 담은 실행 요약입니다.

    Raises:
        저장소 조회 또는 상태 전환 실패는 호출자에게 그대로 전파됩니다.
    """
    now = clock.utc_now()
    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"{SUBSCRIPTION_CANCEL_EXPIRATION_JOB_SCOPE}:{now.date().isoformat()}",
        fencing_counter_key=SUBSCRIPTION_CANCEL_EXPIRATION_JOB_SCOPE,
        now=now,
        ttl=timedelta(minutes=5),
        metadata={
            "api": "internal-billing-run",
            "job_type": "cancel_expiration",
            "limit": limit,
            "dry_run": dry_run,
        },
    )
    try:
        targets = await subscriptions.list_expired_cancel_scheduled_subscriptions(
            now,
            limit,
        )
        if dry_run:
            summary = SubscriptionExpirationRunSummary(
                selected_count=len(targets),
                processed_count=0,
                skipped_count=0,
                failed_count=0,
                cancel_expiration_emails_queued=0,
                expired_subscription_ids=[],
            )
            _log_subscription_expiration_run_summary(summary, now)
            return summary

        processed_ids: list[str] = []
        skipped_count = 0
        for target in targets:
            changed = await _expire_with_audit(
                subscriptions=subscriptions,
                subscription_expiration_uow_factory=(
                    subscription_expiration_uow_factory
                ),
                operator_audits=operator_audits,
                target=target,
                now=now,
            )
            if changed:
                processed_ids.append(target.id)
            else:
                skipped_count += 1

        summary = SubscriptionExpirationRunSummary(
            selected_count=len(targets),
            processed_count=len(processed_ids),
            skipped_count=skipped_count,
            failed_count=0,
            cancel_expiration_emails_queued=len(processed_ids),
            expired_subscription_ids=processed_ids,
        )
        _log_subscription_expiration_run_summary(summary, now)
        return summary
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def _expire_with_audit(
    *,
    subscriptions: SubscriptionExpirationRepository,
    subscription_expiration_uow_factory: SubscriptionExpirationUnitOfWorkFactory | None,
    operator_audits: OperatorAuditRepository | None,
    target: Subscription,
    now: datetime,
) -> bool:
    audit = _cancel_expiration_audit(target, now)
    if subscription_expiration_uow_factory is None:
        changed = await subscriptions.expire_cancel_scheduled_subscription(
            target.id,
            now,
        )
        if changed and operator_audits is not None:
            await operator_audits.save_operator_audit(audit)
        return changed

    async with subscription_expiration_uow_factory() as uow:
        changed = await uow.subscriptions.expire_cancel_scheduled_subscription(
            target.id,
            now,
        )
        if changed:
            await uow.operator_audits.save_operator_audit(audit)
        return changed


def _cancel_expiration_audit(
    subscription: Subscription,
    now: datetime,
) -> OperatorAudit:
    return OperatorAudit(
        id=OperatorAudit.generate_id(),
        operator_id="system:subscription-expiration",
        action="subscription.cancel_expired",
        target_type="subscription",
        target_id=subscription.id,
        previous_state={
            "status": subscription.status,
            "cancel_at": _isoformat(subscription.cancel_at),
            "current_period_end_at": _isoformat(subscription.current_period_end_at),
            "next_billing_at": _isoformat(subscription.next_billing_at),
            "access_until": _isoformat(subscription.access_until),
        },
        next_state={
            "status": "canceled",
            "canceled_at": now.isoformat(),
            "access_until": _isoformat(subscription.current_period_end_at),
            "next_billing_at": None,
            "notification": {
                "template": "subscription_canceled_after_period",
                "queued": True,
            },
        },
        reason_code="cancel_at_period_end_elapsed",
        result="succeeded",
        created_at=now,
    )


def _log_subscription_expiration_run_summary(
    summary: SubscriptionExpirationRunSummary,
    now: datetime,
) -> None:
    logger.info(
        "internal_billing_run_completed",
        extra={
            "payment_job_type": "cancel_expiration",
            "payment_billing_date": now.date().isoformat(),
            "payment_selected": summary.selected_count,
            "payment_processed": summary.processed_count,
            "payment_failed": summary.failed_count,
            "payment_skipped": summary.skipped_count,
            "payment_cancel_expiration_emails_queued": (
                summary.cancel_expiration_emails_queued
            ),
        },
    )


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
