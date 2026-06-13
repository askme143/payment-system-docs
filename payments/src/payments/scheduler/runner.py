from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from datetime import date
from typing import cast

from payments.adapters.mongo.indexes import ensure_mongo_indexes
from payments.application.jobs.subscription_billing import (
    SubscriptionBillingRunCommand,
    run_subscription_billing,
)
from payments.application.jobs.subscription_expiration import (
    expire_cancel_scheduled_subscriptions,
)
from payments.application.notifications import seed_notification_templates_if_empty
from payments.domain.entities.scheduler_run import SchedulerJobType, SchedulerRunLog
from payments.scheduler.composition import (
    build_notification_worker_dependencies,
    build_scheduler_batch_dependencies,
    create_scheduler_mongo_client,
    scheduler_database,
)
from payments.scheduler.config import (
    PaymentSchedulerConfig,
    payment_scheduler_config_from_env,
)
from payments.scheduler.notification_worker import run_notification_worker_once

logger = logging.getLogger(__name__)


async def run_notification_worker_once_from_config(
    config: PaymentSchedulerConfig,
) -> int:
    client = create_scheduler_mongo_client(config)
    try:
        database = scheduler_database(client, config)
        await ensure_mongo_indexes(database)
        dependencies = build_notification_worker_dependencies(database, config)
        await seed_notification_templates_if_empty(
            template_repository=dependencies.template_repository,
            clock=dependencies.clock,
        )
        summary = await run_notification_worker_once(
            dependencies=dependencies,
            worker_id=config.notification_worker_id,
            policy=config.notification_worker_policy,
        )
        logger.info(
            "notification_worker_run_completed",
            extra={
                "notification_selected_count": summary.selected_count,
                "notification_claimed_count": summary.claimed_count,
                "notification_sent_count": summary.sent_count,
                "notification_retry_scheduled_count": (
                    summary.retry_scheduled_count
                ),
                "notification_dead_letter_count": summary.dead_letter_count,
                "notification_skipped_count": summary.skipped_count,
                "notification_failed_count": summary.failed_count,
            },
        )
        return 0 if summary.failed_count == 0 else 1
    finally:
        client.close()


async def run_notification_worker_forever_from_config(
    config: PaymentSchedulerConfig,
) -> int:
    while True:
        await run_notification_worker_once_from_config(config)
        await asyncio.sleep(
            config.notification_worker_policy.poll_interval.total_seconds()
        )


async def run_scheduler_batch_once_from_config(
    config: PaymentSchedulerConfig,
    *,
    job_type: str,
    billing_date: date | None,
    limit: int,
    dry_run: bool,
) -> int:
    client = create_scheduler_mongo_client(config)
    try:
        database = scheduler_database(client, config)
        await ensure_mongo_indexes(database)
        dependencies = build_scheduler_batch_dependencies(database, config)
        now = dependencies.clock.utc_now()
        run = SchedulerRunLog(
            id=SchedulerRunLog.generate_id(),
            job_type=_scheduler_job_type(job_type),
            status="running",
            trigger_source="kubernetes_cronjob",
            worker_id=f"scheduler:{job_type}",
            batch_size=limit,
            billing_date=billing_date,
            dry_run=dry_run,
            started_at=now,
        )
        await dependencies.scheduler_runs.save_scheduler_run(run)
        try:
            if job_type in {"billing", "reminder"}:
                summary = await run_subscription_billing(
                    SubscriptionBillingRunCommand(
                        job_type="reminder" if job_type == "reminder" else "billing",
                        billing_date=billing_date,
                        limit=limit,
                        dry_run=dry_run,
                    ),
                    dependencies.billing_retries,
                    dependencies.payment_customers,
                    dependencies.idempotency_keys,
                    dependencies.payment_provider,
                    dependencies.clock,
                    dependencies.billing_key_cipher,
                    idempotency_key=None,
                    operation_locks=dependencies.operation_locks,
                    subscription_billing_uow_factory=(
                        dependencies.subscription_billing_uow_factory
                    ),
                    notification_dependencies=dependencies.notification_enqueue,
                )
                run.summary = {
                    "selected": summary.processed,
                    "processed": summary.processed,
                    "paid": summary.paid,
                    "failed": summary.failed,
                    "skipped": summary.skipped,
                }
            else:
                expiration_summary = await expire_cancel_scheduled_subscriptions(
                    subscriptions=dependencies.subscription_expirations,
                    clock=dependencies.clock,
                    limit=limit,
                    dry_run=dry_run,
                    operation_locks=dependencies.operation_locks,
                    subscription_expiration_uow_factory=(
                        dependencies.subscription_expiration_uow_factory
                    ),
                    operator_audits=dependencies.operator_audits,
                    notification_dependencies=dependencies.notification_enqueue,
                )
                run.summary = {
                    "selected": expiration_summary.selected_count,
                    "processed": expiration_summary.processed_count,
                    "paid": 0,
                    "failed": expiration_summary.failed_count,
                    "skipped": expiration_summary.skipped_count,
                }
        except Exception:
            finished_at = dependencies.clock.utc_now()
            run.status = "failed"
            run.finished_at = finished_at
            run.duration_ms = int((finished_at - run.started_at).total_seconds() * 1000)
            run.exit_code = 1
            run.error = {
                "code": "SCHEDULER_RUN_FAILED",
                "message": "scheduler run failed",
                "retryable": True,
            }
            await dependencies.scheduler_runs.save_scheduler_run(run)
            raise
        finished_at = dependencies.clock.utc_now()
        run.status = "succeeded"
        run.finished_at = finished_at
        run.duration_ms = int((finished_at - run.started_at).total_seconds() * 1000)
        run.exit_code = 0
        await dependencies.scheduler_runs.save_scheduler_run(run)
        return 0
    finally:
        client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="payments.scheduler.runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "notification-worker",
    )
    subparsers.add_parser(
        "notification-worker-once",
    )
    for command in ("billing", "reminder", "cancel-expiration"):
        batch_parser = subparsers.add_parser(command)
        batch_parser.add_argument("--billing-date")
        batch_parser.add_argument("--limit", "--batch-size", type=int, default=100)
        batch_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    config = payment_scheduler_config_from_env()
    if args.command == "notification-worker":
        return asyncio.run(run_notification_worker_forever_from_config(config))
    if args.command == "notification-worker-once":
        return asyncio.run(run_notification_worker_once_from_config(config))
    return asyncio.run(
        run_scheduler_batch_once_from_config(
            config,
            job_type=_scheduler_job_type(args.command),
            billing_date=(
                date.fromisoformat(args.billing_date)
                if args.billing_date is not None
                else None
            ),
            limit=args.limit,
            dry_run=args.dry_run,
        )
    )


def _scheduler_job_type(value: str) -> SchedulerJobType:
    if value == "cancel-expiration":
        return "cancel_expiration"
    if value in {"billing", "reminder", "cancel_expiration"}:
        return cast(SchedulerJobType, value)
    raise ValueError("scheduler job type is invalid")


if __name__ == "__main__":
    raise SystemExit(main())
