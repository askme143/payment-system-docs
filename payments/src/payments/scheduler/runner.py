from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from payments.adapters.mongo.indexes import ensure_mongo_indexes
from payments.application.notifications import seed_notification_templates_if_empty
from payments.scheduler.composition import (
    build_notification_worker_dependencies,
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="payments.scheduler.runner")
    parser.add_argument(
        "command",
        choices=("notification-worker", "notification-worker-once"),
    )
    args = parser.parse_args(argv)
    config = payment_scheduler_config_from_env()
    if args.command == "notification-worker":
        return asyncio.run(run_notification_worker_forever_from_config(config))
    return asyncio.run(run_notification_worker_once_from_config(config))


if __name__ == "__main__":
    raise SystemExit(main())
