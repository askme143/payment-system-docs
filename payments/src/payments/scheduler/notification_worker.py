from __future__ import annotations

from dataclasses import dataclass

from payments.application.jobs.notifications import (
    NotificationWorkerPolicy,
    NotificationWorkerRunSummary,
    send_due_notifications,
)
from payments.application.ports.clock import Clock
from payments.application.ports.notifications import (
    EmailSender,
    NotificationOutboxRepository,
    NotificationTemplateRepository,
    TemplateArgCipher,
    TemplateRenderer,
)


@dataclass(frozen=True, slots=True)
class NotificationWorkerDependencies:
    outbox_repository: NotificationOutboxRepository
    template_repository: NotificationTemplateRepository
    email_sender: EmailSender
    template_arg_cipher: TemplateArgCipher
    template_renderer: TemplateRenderer
    clock: Clock


async def run_notification_worker_once(
    *,
    dependencies: NotificationWorkerDependencies,
    worker_id: str,
    policy: NotificationWorkerPolicy | None = None,
) -> NotificationWorkerRunSummary:
    return await send_due_notifications(
        outbox_repository=dependencies.outbox_repository,
        template_repository=dependencies.template_repository,
        email_sender=dependencies.email_sender,
        template_arg_cipher=dependencies.template_arg_cipher,
        template_renderer=dependencies.template_renderer,
        clock=dependencies.clock,
        worker_id=worker_id,
        policy=policy,
    )
