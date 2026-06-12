# Email Notification Outbox Design

## Context

The payment backend already records notification intent in several flows, such as subscription billing reminders, billing retry results, and subscription cancellation expiration. Those flows currently expose or audit values like `queued: true`, but they do not yet persist a durable email queue or run a delivery worker.

This design introduces a Mongo-backed notification outbox. Payment and subscription use cases enqueue notification work as part of their normal state changes. A separate worker later renders templates and sends email through an email provider adapter.

## Goals

- Persist email send requests durably before any external email provider call.
- Keep payment and subscription state changes independent from email provider availability.
- Make notification enqueueing idempotent per business event.
- Support product-specific email templates with predictable fallback.
- Allow multiple workers to process the queue safely.
- Keep the design compatible with the existing FastAPI, application, adapter, and scheduler boundaries.

## Non-Goals

- This design does not introduce Celery, Redis, Kafka, or SQS in the first implementation.
- This design does not build a marketing campaign system.
- This design does not add user-facing email preference management.
- This design does not make email delivery part of payment success or failure transaction semantics.

## Recommended Approach

Use a Mongo-backed transactional outbox collection named `notification_outbox`.

Payment and subscription application functions write notification outbox documents through a `NotificationOutboxRepository` port. When the flow already uses a Unit of Work, the notification enqueue write happens inside the same transaction as the payment or subscription state change. The actual email provider call always happens later in a worker.

This keeps the first implementation small and consistent with the current backend. If volume later requires Redis, SQS, or another queue, the application port can stay stable while the adapter changes.

## Domain Model

Add a `NotificationOutboxItem` entity with these core fields:

- `id`
- `event_type`
- `template_key`
- `template_version`
- `product_code`
- `recipient_user_id`
- `recipient_email`
- `payload`
- `status`
- `attempt_count`
- `available_at`
- `locked_until_at`
- `worker_id`
- `provider_message_id`
- `last_error`
- `idempotency_key`
- `created_at`
- `updated_at`
- `sent_at`

Allowed statuses:

- `pending`: ready for delivery when `available_at <= now`.
- `processing`: a worker has claimed the item.
- `sent`: provider accepted the email.
- `retry_scheduled`: a transient failure occurred and the item will be retried.
- `dead_letter`: delivery should no longer be retried automatically.

## Idempotency

Every enqueue operation must include a deterministic `idempotency_key`.

Examples:

- `email:subscription_billing_reminder:{subscription_id}:{billing_cycle_key}`
- `email:subscription_payment_paid:{invoice_id}:{payment_id}`
- `email:subscription_payment_failed:{invoice_id}:{payment_id}`
- `email:subscription_canceled_payment_failed:{subscription_id}:{invoice_id}`
- `email:subscription_canceled_after_period:{subscription_id}:{canceled_at_date}`

`notification_outbox.idempotency_key` gets a unique index. If an enqueue operation is repeated with the same key and same payload, it returns the existing item. If the same key is used with a different payload, the application raises an idempotency conflict.

## Queue Processing

Add an application job such as `send_due_notifications`.

The worker flow is:

1. Select a bounded batch of `pending` or `retry_scheduled` items where `available_at <= now`.
2. Claim each item atomically with a Mongo `findOneAndUpdate` style operation:
   - filter by eligible status and expired or missing lock.
   - set `status = processing`.
   - set `worker_id`.
   - set `locked_until_at = now + processing_ttl`.
   - increment or preserve worker claim metadata as needed.
3. Resolve the template.
4. Validate the payload against the template contract.
5. Render subject, HTML body, and text body.
6. Send through the `EmailSender` port.
7. Mark as `sent` with `provider_message_id` and `sent_at`, or schedule retry/dead letter.

The worker must be a scheduler or queue-worker entrypoint, not a FastAPI background task. The first implementation can expose a scheduler runner command and optionally an authenticated internal route for manual operation.

## Failure Handling

Failures are classified as transient or permanent.

Transient failures include provider timeouts, rate limits, network failures, and provider 5xx responses. The item is updated to:

- `status = retry_scheduled`
- `attempt_count += 1`
- `available_at = now + backoff`
- `last_error` with a concise provider-safe summary

Suggested backoff schedule:

- attempt 1: 1 minute
- attempt 2: 5 minutes
- attempt 3: 30 minutes
- attempt 4: 2 hours
- attempt 5: 12 hours

After the maximum retry count, the item moves to `dead_letter`.

Permanent failures include invalid recipient email, template not found, template payload validation failure, and non-retryable provider 4xx responses. These move directly to `dead_letter`.

Email failure never rolls back payment, invoice, subscription, or audit state. Operational visibility comes from outbox status, worker run summaries, logs, and later admin views.

## Template Resolution

Templates are resolved by event and product context.

Resolution order:

1. Product-specific template: `{product_code}.{event_type}`
2. Product-type template: `{product_type}.{event_type}`
3. Default event template: `default.{event_type}`

Example keys:

- `course_basic.subscription_payment_failed`
- `subscription.subscription_payment_failed`
- `default.subscription_payment_failed`

Add a `notification_templates` collection in the first implementation slice. Product-specific template behavior is part of the core requirement, so storing templates as data is clearer than deploying code for every template change.

Template fields:

- `template_key`
- `event_type`
- `product_code`
- `product_type`
- `subject_template`
- `html_template`
- `text_template`
- `required_payload_keys`
- `status`
- `version`
- `created_at`
- `updated_at`

The selected `template_key` and `template_version` are stored on the outbox item when it is enqueued. This preserves the meaning of already queued emails even if templates change later.

## Application Ports

Add ports under `payments/src/payments/application/ports/notifications.py`:

- `NotificationOutboxRepository`
  - enqueue an outbox item idempotently.
  - claim due items.
  - mark sent.
  - schedule retry.
  - mark dead letter.
- `NotificationTemplateRepository`
  - find active template by resolution candidates.
- `TemplateRenderer`
  - render subject, HTML body, and text body.
- `EmailSender`
  - send a rendered email and return provider message metadata.

Provider SDK types must not leak into application signatures.

## Adapter Placement

Mongo adapters:

- `payments/src/payments/adapters/mongo/notifications.py`
- index additions in `payments/src/payments/adapters/mongo/indexes.py`

Email provider adapters:

- `payments/src/payments/adapters/email.py`
- The first real delivery adapter is `SMTPEmailSender`, configured through environment variables.
- Tests and local development can use `RecordingEmailSender`.

Worker entrypoints:

- `payments/src/payments/application/jobs/notifications.py`
- `payments/src/payments/scheduler/notification_worker.py` or the existing scheduler runner structure.

HTTP, if added, should stay internal/admin-only and should call application jobs rather than sending email directly.

## Mongo Indexes

Recommended indexes:

- unique: `id`
- unique: `idempotency_key`
- processing scan: `(status, available_at, locked_until_at, created_at)`
- recipient lookup: `(recipient_user_id, created_at)`
- dead-letter operations: `(status, updated_at)`
- event lookup: `(event_type, created_at)`

For `notification_templates`:

- unique: `(template_key, version)`
- active lookup: `(event_type, product_code, product_type, status)`

## Existing Flow Integration

Subscription billing reminder:

- Replace the current idempotency-key-only reminder marker with a real outbox enqueue.
- Preserve the same duplicate suppression behavior per subscription and billing cycle.

Subscription payment success:

- Enqueue `subscription_payment_paid` after invoice and payment are saved as paid.
- Include amount, billing date, receipt URL, invoice ID, and subscription ID.

Subscription payment failure:

- Enqueue `subscription_payment_failed` when retry is scheduled.
- Include failure reason summary, retry date, billing method update URL, invoice ID, and subscription ID.

Final payment failure cancellation:

- Enqueue `subscription_canceled_payment_failed`.
- Include canceled date, failure reason summary, manage URL, and resubscribe URL.

Cancel-at-period-end expiration:

- Enqueue `subscription_canceled_after_period` when a cancel-scheduled subscription is finalized as canceled.
- Keep cancellation state and audit log committed even if email later fails.

## Run Summary

The notification worker returns a structured summary:

- `selected_count`
- `claimed_count`
- `sent_count`
- `retry_scheduled_count`
- `dead_letter_count`
- `skipped_count`
- `failed_count`

This mirrors the existing scheduler job style and makes internal/manual runs observable.

## Testing

Application tests:

- enqueue is idempotent with the same payload.
- enqueue conflicts on same idempotency key with different payload.
- worker claims each item once even when locks exist.
- transient provider failure schedules retry with expected backoff.
- permanent render or provider failure marks dead letter.
- sent item records provider message ID and sent timestamp.
- template resolution follows product-specific, product-type, then default fallback.

Adapter tests:

- Mongo unique indexes prevent duplicate enqueue.
- due-item claim is atomic and respects `locked_until_at`.
- retry and dead-letter updates preserve error metadata.

Integration tests:

- subscription billing reminder creates outbox item.
- billing success creates paid notification item.
- retry failure creates failure or cancellation notification item.
- cancellation expiration creates cancellation notification item.

Contract tests:

- public application job functions remain coroutine functions.
- dependencies are explicit ports.
- exported use cases include Korean docstrings with `Args:`, `Returns:`, and `Raises:`.

## First Implementation Decisions

- Use Mongo for both `notification_outbox` and `notification_templates`.
- Implement `SMTPEmailSender` as the first real provider adapter.
- Keep `RecordingEmailSender` for tests and local development.
- Include an internal worker/job entrypoint for sending due notifications.
- Defer admin UI and admin API support for dead-letter replay to a later operations slice. The first implementation exposes dead-letter records through Mongo status, worker summaries, and logs.
