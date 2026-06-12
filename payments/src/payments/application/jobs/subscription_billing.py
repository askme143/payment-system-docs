from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from payments.application.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ProviderError,
)
from payments.application.operation_locks import (
    acquire_required_operation_lock,
    release_operation_lock,
)
from payments.application.ports.billing_keys import BillingKeyCipher
from payments.application.ports.billing_retry import BillingRetryRepository
from payments.application.ports.clock import Clock
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.operation_locks import OperationLockRepository
from payments.application.ports.payment_customers import PaymentCustomerRepository
from payments.application.ports.provider import PaymentProvider
from payments.application.ports.unit_of_work import (
    SubscriptionBillingUnitOfWorkFactory,
)
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan

INTERNAL_BILLING_RUN_IDEMPOTENCY_SCOPE = "internal-billing-run"
SUBSCRIPTION_BILLING_REMINDER_SCOPE = "subscription-billing-reminder"
SubscriptionBillingJobType = Literal["billing", "reminder"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubscriptionBillingRunCommand:
    job_type: SubscriptionBillingJobType = "billing"
    billing_date: date | None = None
    limit: int = 100
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class SubscriptionBillingRunResult:
    billing_date: date
    processed: int
    paid: int
    failed: int
    skipped: int
    excluded_cancel_scheduled: int
    reminder_emails_sent: int
    success_emails_queued: int
    failure_emails_queued: int


async def run_subscription_billing(
    command: SubscriptionBillingRunCommand,
    repository: BillingRetryRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    provider: PaymentProvider,
    clock: Clock,
    billing_key_cipher: BillingKeyCipher,
    idempotency_key: str | None = None,
    operation_locks: OperationLockRepository | None = None,
    subscription_billing_uow_factory: (
        SubscriptionBillingUnitOfWorkFactory | None
    ) = None,
) -> SubscriptionBillingRunResult:
    now = clock.utc_now()
    billing_date = command.billing_date or now.date()
    payload = {
        "jobType": command.job_type,
        "billingDate": billing_date.isoformat(),
        "limit": command.limit,
        "dryRun": command.dry_run,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    existing_key: IdempotencyKey | None = None
    if key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            INTERNAL_BILLING_RUN_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _result_from_response_body(existing_key.response_body)
        if existing_key is not None and existing_key.status == "processing":
            raise InvalidStateTransitionError("subscription billing run is processing")
        await idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=(
                    existing_key.id
                    if existing_key is not None
                    else IdempotencyKey.generate_id()
                ),
                scope=INTERNAL_BILLING_RUN_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="processing",
                created_at=existing_key.created_at if existing_key is not None else now,
                updated_at=now,
                expires_at=now + timedelta(hours=24),
                resource_type="subscription_billing_run",
                resource_id=billing_date.isoformat(),
                locked_until_at=now + timedelta(minutes=5),
            )
        )

    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"internal-billing-run:{command.job_type}:{billing_date.isoformat()}",
        fencing_counter_key="internal-billing-run",
        now=now,
        metadata={
            "api": INTERNAL_BILLING_RUN_IDEMPOTENCY_SCOPE,
            "job_type": command.job_type,
            "billing_date": billing_date.isoformat(),
        },
    )
    try:
        if command.job_type == "reminder":
            result = await _run_reminder_job(
                command,
                repository,
                idempotency_keys,
                billing_date,
                now,
            )
        else:
            result = await _run_billing_job(
                command,
                repository,
                payment_customers,
                provider,
                billing_key_cipher,
                operation_locks,
                subscription_billing_uow_factory,
                now,
                billing_date,
            )

        await _save_successful_idempotency_response(
            idempotency_keys,
            existing_key,
            key_hash,
            request_hash,
            now,
            result,
        )
        _log_subscription_billing_run_result(command.job_type, result)
        return result
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


async def _run_reminder_job(
    command: SubscriptionBillingRunCommand,
    repository: BillingRetryRepository,
    idempotency_keys: IdempotencyKeyRepository,
    billing_date: date,
    now: datetime,
) -> SubscriptionBillingRunResult:
    target_date = billing_date + timedelta(days=7)
    targets = await repository.list_reminder_subscriptions(
        _day_start(target_date),
        _day_end(target_date),
        command.limit,
    )
    sent = 0
    skipped = 0
    if not command.dry_run:
        for subscription in targets:
            plan = await repository.get_subscription_plan(subscription.plan_id)
            if plan is None or plan.status != "active":
                skipped += 1
                continue
            if await _queue_billing_reminder_once(
                subscription=subscription,
                plan=plan,
                billing_date=target_date,
                idempotency_keys=idempotency_keys,
                now=now,
            ):
                sent += 1
            else:
                skipped += 1
    return SubscriptionBillingRunResult(
        billing_date=billing_date,
        processed=len(targets),
        paid=0,
        failed=0,
        skipped=skipped,
        excluded_cancel_scheduled=0,
        reminder_emails_sent=sent,
        success_emails_queued=0,
        failure_emails_queued=0,
    )


async def _queue_billing_reminder_once(
    *,
    subscription: Subscription,
    plan: SubscriptionPlan,
    billing_date: date,
    idempotency_keys: IdempotencyKeyRepository,
    now: datetime,
) -> bool:
    payload = _billing_reminder_payload(subscription, plan, billing_date)
    key_hash = _hash_text(f"{subscription.id}:{billing_date.isoformat()}")
    existing_key = await idempotency_keys.find_idempotency_key(
        SUBSCRIPTION_BILLING_REMINDER_SCOPE,
        key_hash,
    )
    if existing_key is not None:
        return False
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=IdempotencyKey.generate_id(),
            scope=SUBSCRIPTION_BILLING_REMINDER_SCOPE,
            key_hash=key_hash,
            request_hash=_hash_payload(payload),
            status="succeeded",
            created_at=now,
            updated_at=now,
            expires_at=_day_end(billing_date + timedelta(days=45)),
            resource_type="subscription_billing_reminder",
            resource_id=subscription.id,
            response_status=200,
            response_body={
                "reminderSentAt": now,
                "notification": {
                    "template": "subscription_billing_reminder",
                    "payload": payload,
                },
            },
        )
    )
    return True


def _billing_reminder_payload(
    subscription: Subscription,
    plan: SubscriptionPlan,
    billing_date: date,
) -> dict[str, object]:
    return {
        "subscriptionId": subscription.id,
        "userId": subscription.user_id,
        "billingDate": billing_date.isoformat(),
        "amount": plan.amount,
        "currency": plan.currency,
        "planName": _plan_display_name(plan),
        "subscriptionManageUrl": "/subscriptions/me",
    }


async def _run_billing_job(
    command: SubscriptionBillingRunCommand,
    repository: BillingRetryRepository,
    payment_customers: PaymentCustomerRepository,
    provider: PaymentProvider,
    billing_key_cipher: BillingKeyCipher,
    operation_locks: OperationLockRepository | None,
    subscription_billing_uow_factory: SubscriptionBillingUnitOfWorkFactory | None,
    now: datetime,
    billing_date: date,
) -> SubscriptionBillingRunResult:
    billing_cutoff_at = _day_end(billing_date)
    targets = await repository.list_due_active_subscriptions(
        billing_cutoff_at,
        command.limit,
    )
    excluded = await repository.count_excluded_billing_subscriptions()
    if command.dry_run:
        return SubscriptionBillingRunResult(
            billing_date=billing_date,
            processed=len(targets),
            paid=0,
            failed=0,
            skipped=0,
            excluded_cancel_scheduled=excluded,
            reminder_emails_sent=0,
            success_emails_queued=0,
            failure_emails_queued=0,
        )

    paid = 0
    failed = 0
    skipped = 0
    success_emails = 0
    failure_emails = 0
    billing_at = _day_start(billing_date)
    for subscription in targets:
        expected_next_billing_at = subscription.next_billing_at
        if expected_next_billing_at is None:
            skipped += 1
            continue
        subscription = await _current_billable_subscription(
            repository,
            subscription.id,
            expected_next_billing_at,
        )
        if subscription is None:
            skipped += 1
            continue
        _apply_pending_plan_for_billing(subscription, billing_cutoff_at)
        plan = await repository.get_subscription_plan(subscription.plan_id)
        if plan is None or plan.status != "active":
            skipped += 1
            continue
        billing_cycle_key = Payment.generate_billing_cycle_key(
            subscription.id,
            billing_at,
        )
        if billing_cycle_key is None:
            skipped += 1
            continue
        existing_invoice = await repository.get_invoice_by_billing_cycle(
            subscription.id,
            billing_cycle_key,
        )
        if existing_invoice is not None:
            skipped += 1
            continue

        billing_cycle_lock = await _acquire_billing_cycle_lock(
            operation_locks=operation_locks,
            subscription=subscription,
            billing_cycle_key=billing_cycle_key,
            now=now,
        )
        if operation_locks is not None and billing_cycle_lock is None:
            skipped += 1
            continue
        try:
            existing_invoice = await repository.get_invoice_by_billing_cycle(
                subscription.id,
                billing_cycle_key,
            )
            if existing_invoice is not None:
                skipped += 1
                continue

            payment = _draft_payment(subscription, plan, billing_cycle_key, now)
            invoice = _issued_invoice(subscription, payment, billing_cycle_key, now)
            billing_method = await repository.get_default_billing_method(
                subscription.user_id
            )
            if billing_method is not None:
                payment.billing_method_id = billing_method.id
            instrument = (
                await repository.get_payment_instrument(billing_method.instrument_id)
                if billing_method is not None
                else None
            )
            payment_customer = (
                await payment_customers.get_active_payment_customer_for_user(
                    subscription.user_id
                )
            )
            if (
                billing_method is None
                or instrument is None
                or instrument.status != "active"
                or payment_customer is None
                or payment_customer.id != subscription.payment_customer_id
                or payment_customer.id != billing_method.payment_customer_id
                or payment_customer.id != instrument.payment_customer_id
            ):
                _mark_billing_failure(
                    payment,
                    invoice,
                    subscription,
                    now,
                    "BILLING_METHOD_NOT_CHARGEABLE",
                    "default billing method is not chargeable",
                    retryable=False,
                )
                if await _save_billing_documents(
                    repository,
                    payment,
                    invoice,
                    subscription,
                    expected_next_billing_at,
                    subscription_billing_uow_factory,
                ):
                    failed += 1
                    failure_emails += 1
                else:
                    skipped += 1
                continue

            try:
                charged = await provider.charge_billing_key(
                    billing_key=billing_key_cipher.decrypt(instrument.billing_key),
                    customer_key=payment_customer.customer_key,
                    order_id=payment.order_id,
                    amount=payment.amount,
                    order_name=f"Subscription billing {subscription.id}",
                    idempotency_key=billing_cycle_key,
                )
            except ProviderError as exc:
                _mark_billing_failure(
                    payment,
                    invoice,
                    subscription,
                    now,
                    exc.provider_code or "PROVIDER_BILLING_CHARGE_FAILED",
                    str(exc),
                    retryable=exc.retryable,
                    reason=(
                        "provider_rejected"
                        if exc.provider_code
                        else "provider_error"
                    ),
                )
                if await _save_billing_documents(
                    repository,
                    payment,
                    invoice,
                    subscription,
                    expected_next_billing_at,
                    subscription_billing_uow_factory,
                ):
                    failed += 1
                    failure_emails += 1
                else:
                    skipped += 1
                continue
            if charged.order_id != payment.order_id or charged.amount != payment.amount:
                _mark_billing_failure(
                    payment,
                    invoice,
                    subscription,
                    now,
                    "PROVIDER_BILLING_CHARGE_MISMATCH",
                    "provider billing charge response does not match request",
                    retryable=True,
                    reason="provider_error",
                )
                if await _save_billing_documents(
                    repository,
                    payment,
                    invoice,
                    subscription,
                    expected_next_billing_at,
                    subscription_billing_uow_factory,
                ):
                    failed += 1
                    failure_emails += 1
                else:
                    skipped += 1
                continue

            payment.status = "paid"
            payment.payment_key = charged.payment_key
            payment.approved_at = charged.approved_at
            payment.receipt_url = charged.receipt_url
            payment.method = charged.method
            payment.method_detail = charged.method_detail
            payment.provider_response_summary = charged.response_summary
            invoice.status = "paid"
            invoice.receipt_url = charged.receipt_url
            subscription.status = "active"
            subscription.current_period_start_at = billing_at
            subscription.current_period_end_at = _next_billing_at(
                billing_at,
                plan.billing_period,
            )
            subscription.next_billing_at = subscription.current_period_end_at
            if await _save_billing_documents(
                repository,
                payment,
                    invoice,
                    subscription,
                    expected_next_billing_at,
                    subscription_billing_uow_factory,
                ):
                paid += 1
                success_emails += 1
            else:
                skipped += 1
        finally:
            await release_operation_lock(
                operation_locks=operation_locks,
                operation_lock=billing_cycle_lock,
                released_at=now,
            )

    return SubscriptionBillingRunResult(
        billing_date=billing_date,
        processed=paid + failed + skipped,
        paid=paid,
        failed=failed,
        skipped=skipped,
        excluded_cancel_scheduled=excluded,
        reminder_emails_sent=0,
        success_emails_queued=success_emails,
        failure_emails_queued=failure_emails,
    )


async def _acquire_billing_cycle_lock(
    *,
    operation_locks: OperationLockRepository | None,
    subscription: Subscription,
    billing_cycle_key: str,
    now: datetime,
):
    try:
        return await acquire_required_operation_lock(
            operation_locks=operation_locks,
            lock_key=f"subscription-billing:{subscription.id}:{billing_cycle_key}",
            fencing_counter_key="subscription-billing",
            now=now,
            metadata={
                "api": INTERNAL_BILLING_RUN_IDEMPOTENCY_SCOPE,
                "job_type": "billing",
                "subscription_id": subscription.id,
                "billing_cycle_key": billing_cycle_key,
            },
        )
    except InvalidStateTransitionError:
        return None


def _draft_payment(
    subscription: Subscription,
    plan: SubscriptionPlan,
    billing_cycle_key: str,
    now: datetime,
) -> Payment:
    return Payment(
        id=Payment.generate_id(),
        order_id=Payment.generate_id(),
        amount=plan.amount,
        status="ready",
        created_at=now,
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
        payment_customer_id=subscription.payment_customer_id,
    )


def _issued_invoice(
    subscription: Subscription,
    payment: Payment,
    billing_cycle_key: str,
    now: datetime,
) -> Invoice:
    return Invoice(
        id=Invoice.generate_id(),
        user_id=subscription.user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=now,
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
    )


def _mark_billing_failure(
    payment: Payment,
    invoice: Invoice,
    subscription: Subscription,
    now: datetime,
    provider_code: str,
    message: str,
    *,
    retryable: bool = True,
    reason: str = "provider_error",
) -> None:
    payment.status = "failed"
    payment.failure = {
        "phase": "confirm",
        "reason": reason,
        "providerCode": provider_code,
        "message": message,
        "retryable": retryable,
    }
    payment.retry_scheduled_at = now + timedelta(days=1) if retryable else None
    invoice.status = "issued"
    subscription.status = "past_due"


async def _save_billing_documents(
    repository: BillingRetryRepository,
    payment: Payment,
    invoice: Invoice,
    subscription: Subscription,
    expected_next_billing_at: datetime,
    subscription_billing_uow_factory: SubscriptionBillingUnitOfWorkFactory | None,
) -> bool:
    if subscription_billing_uow_factory is not None:
        async with subscription_billing_uow_factory() as uow:
            return await uow.billing.save_subscription_billing_result(
                payment=payment,
                invoice=invoice,
                subscription=subscription,
                expected_next_billing_at=expected_next_billing_at,
            )
    return await repository.save_subscription_billing_result(
        payment=payment,
        invoice=invoice,
        subscription=subscription,
        expected_next_billing_at=expected_next_billing_at,
    )


async def _current_billable_subscription(
    repository: BillingRetryRepository,
    subscription_id: str,
    expected_next_billing_at: datetime,
) -> Subscription | None:
    subscription = await repository.get_subscription(subscription_id)
    if subscription is None:
        return None
    if subscription.status != "active":
        return None
    if subscription.next_billing_at != expected_next_billing_at:
        return None
    return replace(subscription)


def _apply_pending_plan_for_billing(
    subscription: Subscription,
    billing_cutoff_at: datetime,
) -> None:
    if (
        subscription.pending_plan_id is None
        or subscription.pending_plan_effective_at is None
        or subscription.pending_plan_effective_at > billing_cutoff_at
    ):
        return
    subscription.plan_id = subscription.pending_plan_id
    subscription.pending_plan_id = None
    subscription.pending_plan_effective_at = None


def _next_billing_at(current: datetime, billing_period: str) -> datetime:
    days = 365 if billing_period == "yearly" else 30
    return current + timedelta(days=days)


def _plan_display_name(plan: SubscriptionPlan) -> str:
    period_label = "월간" if plan.billing_period == "monthly" else "연간"
    parts = [part for part in plan.plan_code.split("_") if part]
    if parts and parts[-1].casefold() == plan.billing_period.casefold():
        parts = parts[:-1]
    base = " ".join(part.capitalize() for part in parts)
    return f"{base} {period_label}".strip()


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _day_end(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=UTC)


async def _save_successful_idempotency_response(
    idempotency_keys: IdempotencyKeyRepository,
    existing_key: IdempotencyKey | None,
    key_hash: str | None,
    request_hash: str,
    now: datetime,
    result: SubscriptionBillingRunResult,
) -> None:
    if key_hash is None:
        return
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=(
                existing_key.id
                if existing_key is not None
                else IdempotencyKey.generate_id()
            ),
            scope=INTERNAL_BILLING_RUN_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            status="succeeded",
            created_at=existing_key.created_at if existing_key is not None else now,
            updated_at=now,
            expires_at=(
                existing_key.expires_at
                if existing_key is not None
                else now + timedelta(hours=24)
            ),
            resource_type="subscription_billing_run",
            resource_id=result.billing_date.isoformat(),
            response_status=200,
            response_body=_result_to_response_body(result),
        )
    )


def _result_to_response_body(
    result: SubscriptionBillingRunResult,
) -> dict[str, object]:
    return {
        "billingDate": result.billing_date.isoformat(),
        "processed": result.processed,
        "paid": result.paid,
        "failed": result.failed,
        "skipped": result.skipped,
        "excludedCancelScheduled": result.excluded_cancel_scheduled,
        "reminderEmailsSent": result.reminder_emails_sent,
        "successEmailsQueued": result.success_emails_queued,
        "failureEmailsQueued": result.failure_emails_queued,
    }


def _log_subscription_billing_run_result(
    job_type: SubscriptionBillingJobType,
    result: SubscriptionBillingRunResult,
) -> None:
    logger.info(
        "internal_billing_run_completed",
        extra={
            "payment_job_type": job_type,
            "payment_billing_date": result.billing_date.isoformat(),
            "payment_processed": result.processed,
            "payment_paid": result.paid,
            "payment_failed": result.failed,
            "payment_skipped": result.skipped,
            "payment_excluded_cancel_scheduled": (
                result.excluded_cancel_scheduled
            ),
            "payment_reminder_emails_sent": result.reminder_emails_sent,
            "payment_success_emails_queued": result.success_emails_queued,
            "payment_failure_emails_queued": result.failure_emails_queued,
        },
    )


def _result_from_response_body(
    body: Mapping[str, object],
) -> SubscriptionBillingRunResult:
    return SubscriptionBillingRunResult(
        billing_date=date.fromisoformat(str(body["billingDate"])),
        processed=_response_int(body["processed"]),
        paid=_response_int(body["paid"]),
        failed=_response_int(body["failed"]),
        skipped=_response_int(body["skipped"]),
        excluded_cancel_scheduled=_response_int(body["excludedCancelScheduled"]),
        reminder_emails_sent=_response_int(body["reminderEmailsSent"]),
        success_emails_queued=_response_int(body["successEmailsQueued"]),
        failure_emails_queued=_response_int(body["failureEmailsQueued"]),
    )


def _response_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise InvalidStateTransitionError("idempotency response is invalid")


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
