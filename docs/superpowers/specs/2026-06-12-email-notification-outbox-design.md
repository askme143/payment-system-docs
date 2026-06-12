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

Recipient email addresses and minimal personalization profile fields are
resolved at enqueue time through a `NotificationRecipientResolver` port.
Application use cases must not read user, admin, or profile databases directly
for notification recipient data. A temporary Mongo-backed resolver is
acceptable as an adapter implementation while the member/profile API is not
ready, but the application contract must stay the same so it can later be
replaced by an HTTP/member-service adapter.

## Domain Model

Add a `NotificationOutboxItem` entity backed by `notification_outbox`.
The outbox stores delivery metadata and `template_args`, not arbitrary
business payload. Payment, invoice, subscription, and auth collections remain
the source of truth for business state.

`payload` is not used as a field name in the first implementation. Use
`template_args` consistently in domain entities, repository ports, Mongo
documents, worker code, logs, and tests.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `_id` | yes | `UuidString` | MongoDB document ID and application-generated outbox item ID. |
| `idempotency_key` | yes | `string` | Deterministic business event identity. Unique across outbox items. |
| `idempotency_payload_hash` | yes | `string` | Canonical hash of the enqueue content used to detect same-key content conflicts. |
| `event_type` | yes | `string` | Notification event, such as `subscription_payment_failed` or `admin_auth.login_link`. |
| `recipient_type` | yes | `string` | `user`, `admin`, or `external`. |
| `recipient_user_id` | no | `ExternalUserId` | Set when `recipient_type = user`. |
| `recipient_admin_id` | no | `UuidString` | Set when `recipient_type = admin`. |
| `recipient_email` | yes | `string` | Actual delivery address. |
| `product_code` | no | `string` | Product-specific template resolution context. |
| `product_type` | no | `string` | Product-type template resolution context. |
| `template_key` | yes | `string` | Template key selected at enqueue time. |
| `template_version` | yes | `number` | Template version selected at enqueue time. |
| `template_args` | yes | `object` | Minimal rendering arguments required by the selected template. Non-sensitive billing values may be plain JSON; sensitive auth values must be field-level encrypted value objects. |
| `status` | yes | `string` | Delivery lifecycle status. |
| `attempt_count` | yes | `number` | Number of provider send attempts. Initial value is `0`. |
| `available_at` | yes | `Date` | Earliest time a worker may process the item. |
| `locked_until_at` | no | `Date` | Claim lock expiry for processing recovery. |
| `worker_id` | no | `string` | Worker that currently owns or last owned the item. |
| `provider_message_id` | no | `string` | Provider message identifier returned after accepted delivery. |
| `last_error` | no | `object` | Provider-safe summary: `code`, `message`, `retryable`, `occurred_at`. |
| `expires_at` | no | `Date` | Send eligibility deadline. Workers must not send after this time. |
| `sent_at` | no | `Date` | Time the provider accepted the email. |
| `created_at` | yes | `Date` | Creation time. |
| `updated_at` | yes | `Date` | Last mutation time. |
| `purge_after_at` | no | `Date` | Mongo TTL deletion time. This is separate from `expires_at`. |

Allowed statuses:

- `pending`: ready for delivery when `available_at <= now`.
- `processing`: a worker has claimed the item.
- `sent`: provider accepted the email.
- `retry_scheduled`: a transient failure occurred and the item will be retried.
- `dead_letter`: delivery should no longer be retried automatically.

## Idempotency

Every enqueue operation must include a deterministic `idempotency_key`.

All keys use `email:{event_type}:...`. The suffix must use stable business
source IDs only. Do not include recipient email, recipient name, product code,
product type, selected template, amount, failure text, or other display content
in the key. If a needed source ID does not exist yet, create a stable event ID
such as `cancel_id` or `operator_audit_id` before enqueueing; do not substitute
timestamps or random values at enqueue time.

| Event type | idempotency_key format | Source ID |
| --- | --- | --- |
| `admin_auth.login_link` | `email:admin_auth.login_link:{admin_auth_token_id}` | `admin_auth_tokens._id` |
| `admin_auth.password_reset` | `email:admin_auth.password_reset:{admin_auth_token_id}` | `admin_auth_tokens._id` |
| `subscription_billing_reminder` | `email:subscription_billing_reminder:{subscription_id}:{billing_cycle_key}` | `subscriptions._id + invoices.billing_cycle_key` |
| `subscription_payment_paid` | `email:subscription_payment_paid:{invoice_id}:{payment_id}` | `invoices._id + payments._id` |
| `subscription_payment_failed` | `email:subscription_payment_failed:{invoice_id}:{payment_id}` | `invoices._id + failed payments._id` |
| `subscription_canceled_payment_failed` | `email:subscription_canceled_payment_failed:{subscription_id}:{invoice_id}` | `subscriptions._id + terminal failed invoices._id` |
| `subscription_canceled_after_period` | `email:subscription_canceled_after_period:{subscription_id}:{period_end_at_date}` | `subscriptions._id + period_end_at YYYY-MM-DD` |
| `subscription_plan_upgrade_receipt` | `email:subscription_plan_upgrade_receipt:{invoice_id}:{payment_id}` | `upgrade invoices._id + payments._id` |
| `payment_cancel_completed` | `email:payment_cancel_completed:{payment_id}:{cancel_id}` | `payments._id + cancelHistory.cancelId` |
| `subscription_adjustment_completed` | `email:subscription_adjustment_completed:{subscription_id}:{operator_audit_id}` | `subscriptions._id + operator_audits._id` |
| `one_time_payment_paid` | `email:one_time_payment_paid:{checkout_id}:{payment_id}` | `checkouts._id + payments._id` |

`notification_outbox.idempotency_key` gets a unique index.
`idempotency_key` identifies the business event; `idempotency_payload_hash`
verifies that repeated enqueue attempts for that event carry the same delivery
content.

The payload hash is calculated from a canonical representation of the enqueue
content, including:

- `event_type`
- `recipient_type`
- `recipient_user_id`
- `recipient_admin_id`
- `recipient_email`
- `product_code`
- `product_type`
- `template_key`
- `template_version`
- `template_args`
- `expires_at`

The hash must not depend on randomized encrypted output. For sensitive template
arguments, calculate the hash before encryption or replace the raw sensitive
value with a deterministic non-reversible digest in the canonical hash input.

If an enqueue operation is repeated with the same `idempotency_key` and the
same `idempotency_payload_hash`, it returns the existing item. If the same key
is used with a different hash, the application raises an idempotency conflict.

## Recipient Resolution

Add a `NotificationRecipientResolver` port under
`payments/src/payments/application/ports/notifications.py`.

Suggested port shape:

```python
class NotificationRecipientResolver(Protocol):
    async def resolve_user(self, user_id: str) -> ResolvedNotificationRecipient:
        ...

    async def resolve_admin(
        self,
        admin_id: str,
    ) -> ResolvedNotificationRecipient:
        ...
```

`ResolvedNotificationRecipient` includes the `recipient_type`, source recipient
id, `email`, and optional display name only:

- `recipientName`

The resolved email is copied into `notification_outbox.recipient_email` at
enqueue time. `recipientName` is a common optional template argument for all
templates. It is copied from the resolved recipient name when available;
templates must fall back to generic copy such as "customer" or "administrator"
when it is absent. `locale`, `timezone`, and email receive-preference fields are
not part of the current payment email contract. The worker must not call the
resolver again.

The resolver must stay narrow. It may return `email` and `recipientName`, but it
must not return locale, timezone, payment state, subscription state, billing
keys, provider customer keys, phone numbers, addresses, or arbitrary
template-specific business payload.

Resolver implementation policy:

- The first adapter may read Mongo or a local projection while the member API is
  not available.
- That direct data-source access stays inside the adapter. Application use cases
  depend only on the port.
- A future HTTP/member-service adapter must be swappable without changing
  payment, subscription, auth, or notification application code.
- Provider/network calls and slow profile lookups should not happen inside a
  Mongo transaction. Resolve the recipient before the short transaction that
  writes business state plus the outbox item.

Failure policy:

- Payment and subscription notification recipient resolution failure must not
  roll back already-valid payment state. Record a skipped notification or a
  `dead_letter` item with `last_error.code = recipient_unresolved`, depending on
  the use-case boundary.
- Auth email recipient resolution failure is part of the auth flow. Revoke or
  expire the generated auth token and return a stable failure rather than
  leaving an active token without a deliverable email.

## Template Argument Security

`template_args` remains an object. Do not encrypt the entire object because the
worker and operations need to validate required argument names and diagnose
dead-letter causes. Encrypt only sensitive fields.

Plain values are allowed for non-sensitive billing and product display data,
such as amount, invoice ID, subscription ID, billing date, retry date, and
receipt/manage URLs that do not embed secrets.

Sensitive auth values must be stored as field-level encrypted value objects and
decrypted only inside the notification worker immediately before rendering.
Examples include login links, reset tokens, OTP codes, magic links, and any
future one-time credential.

Encrypted value shape:

```json
{
  "_encrypted": true,
  "value": "opaque-encrypted-value"
}
```

Field rules:

- `_encrypted` is always `true`.
- `value` is an opaque string produced by the encryption module.
- The outbox document does not store crypto internals. Algorithm and key
  selection are server configuration and encryption-module responsibilities.
- If key rotation becomes necessary later, add a new optional field or envelope
  version at that time; do not design that complexity into the first contract.

If the envelope is malformed or decryption fails, the worker must not call the
provider. Mark the item `dead_letter` with
`last_error.code = template_arg_decrypt_failed`.

Logs, worker summaries, `last_error`, and test assertion messages must not
include decrypted sensitive values. They should use argument names, error codes,
or masked values only.

## Queue Processing

Add an application job such as `send_due_notifications`.

Worker constants:

- `batch_size`: 100
- `claim_limit_per_run`: 100
- `poll_interval`: 10 seconds
- `lock_duration`: 5 minutes
- `max_attempts`: 5
- `backoff_schedule`: 1 minute, 5 minutes, 30 minutes, 2 hours, 12 hours

The worker flow is:

1. Select a bounded batch of `pending` or `retry_scheduled` items where `available_at <= now`.
2. Claim each item atomically with a Mongo `findOneAndUpdate` style operation:
   - filter by eligible status and expired or missing lock.
   - set `status = processing`.
   - set `worker_id`.
   - set `locked_until_at = now + 5 minutes`.
   - increment `attempt_count`.
3. Load the stored `template_key` and `template_version`.
4. Validate `template_args` against the template contract.
5. Render subject, HTML body, and text body.
6. Send through the `EmailSender` port.
7. Mark as `sent` with `provider_message_id` and `sent_at`, or schedule retry/dead letter.

The worker must be a scheduler or queue-worker entrypoint, not a FastAPI background task. The first implementation can expose a scheduler runner command and optionally an authenticated internal route for manual operation.

## Failure Handling

Failures are classified as transient or permanent.

Transient failures include SMTP timeouts, connection errors, temporary DNS or
network failures, and SMTP 4xx transient responses. The item is updated to:

- `status = retry_scheduled`
- `attempt_count += 1`
- `available_at = now + backoff`
- `last_error` with a concise provider-safe summary

Backoff schedule:

- attempt 1: 1 minute
- attempt 2: 5 minutes
- attempt 3: 30 minutes
- attempt 4: 2 hours
- attempt 5: 12 hours

After `max_attempts = 5`, the item moves to `dead_letter`.

Permanent failures include invalid recipient email, template not found, template
argument validation failure, Jinja2 render errors, encrypted value decryption
failure, expired `expires_at`, and SMTP 5xx permanent responses such as invalid
recipient or rejected sender. These move directly to `dead_letter`.

Email failure never rolls back payment, invoice, subscription, or audit state. Operational visibility comes from outbox status, worker run summaries, logs, and later admin views.

## Template Resolution

Templates are resolved by event and product context at enqueue time.

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
- `required_template_args`
- `status`
- `version`
- `created_at`
- `updated_at`

The selected `template_key` and `template_version` are stored on the outbox item when it is enqueued. This preserves the meaning of already queued emails even if templates change later. The worker loads the stored key/version and does not re-resolve fallback candidates.

## Template Rendering

Templates use Jinja2.

- `subject_template`, `html_template`, and `text_template` all use Jinja2
  syntax.
- Use `StrictUndefined` so missing values fail rendering instead of silently
  becoming empty strings.
- `html_template autoescape` is enabled.
- `subject_template` and `text_template` do not use HTML autoescape.
- Validate `required_template_args` and decrypt `encryptedArgs` before rendering.
- Jinja2 syntax errors, `StrictUndefined` errors, and render errors move the
  item to `dead_letter` with `last_error.code = template_render_failed`.
- The rendering context is limited to `template_args` and explicit safe helpers.
  Templates must not perform DB reads, provider calls, or arbitrary function
  calls.

## Initial Template Seed

Initial notification templates are generated with simple default copy.

- If `notification_templates` is empty, initialize every catalog event as
  `default.{event_type}`, `version = 1`, `status = active`.
- If at least one template already exists, startup/init must not automatically
  overwrite existing templates.
- Seed templates include `subject_template`, `html_template`, and
  `text_template`.
- Each seed template must reference its event's `required_template_args` so the
  contract is exercised by rendering tests.
- Product-specific `product_code` and `product_type` templates are not part of
  the initial seed; add them later through operations tooling or migrations.

## Initial Template Catalog

Use camelCase for `template_args`. `recipientName` and `supportUrl` are common
optional arguments for every template. `recipientName` replaces event-specific
names such as `adminName` or `userName`.

| Event type | Required template args | Optional template args | Encrypted args |
| --- | --- | --- | --- |
| `admin_auth.login_link` | `loginLink`, `expiresMinutes` | `recipientName`, `requestIp`, `userAgent`, `supportUrl` | `loginLink` |
| `admin_auth.password_reset` | `resetLink`, `expiresMinutes` | `recipientName`, `requestIp`, `supportUrl` | `resetLink` |
| `subscription_billing_reminder` | `subscriptionId`, `planName`, `amount`, `currency`, `billingDate`, `subscriptionManageUrl` | `recipientName`, `productName`, `billingMethodSummary`, `supportUrl` | none |
| `subscription_payment_paid` | `subscriptionId`, `invoiceId`, `amount`, `currency`, `billingDate`, `receiptUrl` | `recipientName`, `planName`, `productName`, `paidAt`, `paymentMethodSummary`, `supportUrl` | none |
| `subscription_payment_failed` | `subscriptionId`, `invoiceId`, `amount`, `currency`, `failureSummary`, `retryScheduledAt`, `billingMethodUpdateUrl` | `recipientName`, `planName`, `productName`, `providerCode`, `supportUrl` | none |
| `subscription_canceled_payment_failed` | `subscriptionId`, `invoiceId`, `canceledAt`, `failureSummary`, `cancelReason`, `subscriptionManageUrl`, `resubscribeUrl` | `recipientName`, `amount`, `currency`, `providerCode`, `planName`, `productName`, `supportUrl` | none |
| `subscription_canceled_after_period` | `subscriptionId`, `periodEndAt`, `canceledAt`, `accessUntil`, `resubscribeUrl` | `recipientName`, `planName`, `productName`, `subscriptionManageUrl`, `supportUrl` | none |
| `subscription_plan_upgrade_receipt` | `subscriptionId`, `invoiceId`, `paymentId`, `fromPlanName`, `toPlanName`, `amount`, `currency`, `changedAt`, `receiptUrl` | `recipientName`, `effectiveAt`, `paymentMethodSummary`, `supportUrl` | none |
| `payment_cancel_completed` | `paymentId`, `cancelAmount`, `currency`, `canceledAt` | `recipientName`, `invoiceId`, `orderName`, `cancelReason`, `receiptUrl`, `supportUrl` | none |
| `subscription_adjustment_completed` | `subscriptionId`, `adjustmentType`, `status`, `adjustedAt` | `recipientName`, `previousStatus`, `nextBillingAt`, `accessUntil`, `reasonSummary`, `subscriptionManageUrl`, `supportUrl` | none |
| `one_time_payment_paid` | `checkoutId`, `paymentId`, `orderName`, `amount`, `currency`, `paidAt`, `receiptUrl` | `recipientName`, `itemSummary`, `paymentMethodSummary`, `supportUrl` | none |

Do not include `recipientEmail`, `recipientType`, `templateKey`, or
`templateVersion` in `template_args`; those are outbox metadata. Do not include
provider raw responses, billing keys, card numbers, provider secrets, or raw auth
token hashes in any template argument.

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
- `NotificationRecipientResolver`
  - resolve user/admin recipient profile snapshots at enqueue time.
- `TemplateRenderer`
  - render subject, HTML body, and text body.
- `EmailSender`
  - send a rendered email and return provider message metadata.
  - keep the application contract provider-neutral, but implement SMTP as the
    first real delivery adapter.

Provider SDK types must not leak into application signatures.

## Adapter Placement

Mongo adapters:

- `payments/src/payments/adapters/mongo/notifications.py`
- index additions in `payments/src/payments/adapters/mongo/indexes.py`
- a temporary Mongo-backed recipient resolver adapter if the member/profile API
  is not ready.

Email provider adapters:

- `payments/src/payments/adapters/email.py`
- The first real delivery adapter is `SMTPEmailSender`, configured through environment variables.
- Do not implement SES in the first slice. If SES is adopted later, add a
  separate adapter behind the same `EmailSender` port without changing the
  outbox schema or worker contract.
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
- recipient lookup: `(recipient_type, recipient_user_id, created_at)`
- admin recipient lookup: `(recipient_type, recipient_admin_id, created_at)`
- dead-letter operations: `(status, updated_at)`
- event lookup: `(event_type, created_at)`
- template lookup/debug: `(template_key, template_version, created_at)`
- TTL: `purge_after_at` with `expireAfterSeconds = 0`

For `notification_templates`:

- unique: `(template_key, version)`
- active lookup: `(event_type, product_code, product_type, status)`

## Retention Policy

`expires_at` and `purge_after_at` have different meanings.

- `expires_at`: send eligibility deadline. If `expires_at <= now`, the worker
  marks the item `dead_letter` and does not call the provider.
- `purge_after_at`: Mongo TTL deletion deadline. It controls storage
  retention only and must not be used to decide whether an email can be sent.

Retention defaults:

- `pending` and `retry_scheduled`: keep until delivery completes or until an
  explicit operational retention deadline. Auth emails use a short
  `expires_at`.
- `processing`: do not TTL-delete solely because the item is processing.
  Recovery uses `locked_until_at`.
- `sent`: set `purge_after_at = sent_at + 90 days`.
- `dead_letter`: set `purge_after_at = updated_at + 180 days`.
- auth email items: set `expires_at` to the auth token expiry and
  `purge_after_at` no later than `expires_at + 1 day`.

## Existing Flow Integration

Subscription billing reminder:

- Replace the current idempotency-key-only reminder marker with a real outbox enqueue.
- Preserve the same duplicate suppression behavior per subscription and billing cycle.

Subscription payment success:

- Enqueue `subscription_payment_paid` after invoice and payment are saved as paid.
- Include amount, billing date, receipt URL, invoice ID, and subscription ID in `template_args`.

Subscription payment failure:

- Enqueue `subscription_payment_failed` when retry is scheduled.
- Include failure reason summary, retry date, billing method update URL, invoice ID, and subscription ID in `template_args`.

Final payment failure cancellation:

- Enqueue `subscription_canceled_payment_failed`.
- Include canceled date, failure reason summary, manage URL, and resubscribe URL in `template_args`.

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

- enqueue is idempotent with the same `idempotency_payload_hash`.
- enqueue conflicts on same idempotency key with different hash.
- recipient email is resolved at enqueue time and stored as an outbox snapshot.
- payment/subscription recipient resolution failure does not roll back valid
  business state.
- auth recipient resolution failure revokes or expires generated auth tokens.
- sensitive `template_args` are encrypted at field level and never appear in
  logs, summaries, or `last_error`.
- idempotency hash remains stable when encrypted fields use randomized
  ciphertext.
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
- Use `template_args` as the only rendering-argument field name. Do not add a
  `payload` field to `notification_outbox`.
- Select and store `template_key` and `template_version` at enqueue time.
- Use `idempotency_key` for business-event identity and
  `idempotency_payload_hash` for same-key content conflict detection.
- Separate `expires_at` for send eligibility from `purge_after_at` for TTL
  deletion.
- Implement `SMTPEmailSender` as the first real provider adapter.
- Keep SES/API-based delivery out of the first slice. A later SES adapter must
  reuse the same `EmailSender` port and preserve the outbox schema.
- Keep `RecordingEmailSender` for tests and local development.
- Include an internal worker/job entrypoint for sending due notifications.
- Defer admin UI and admin API support for dead-letter replay to a later operations slice. The first implementation exposes dead-letter records through Mongo status, worker summaries, and logs.
