from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from payments.application.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ProviderError,
    ResourceNotFoundError,
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
from payments.domain.entities.ids import generate_uuid_id
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription

INTERNAL_BILLING_RETRY_IDEMPOTENCY_SCOPE = "internal-billing-retry"
MAX_BILLING_RETRY_FAILED_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class BillingRetryCommand:
    force: bool = False
    reason: str | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class BillingRetryResult:
    invoice_id: str
    subscription_id: str
    status: str
    invoice_status: str
    payment_status: str
    next_billing_date: datetime | None
    receipt_url: str | None
    notification: dict[str, object]


async def retry_subscription_billing(
    invoice_id: str,
    command: BillingRetryCommand,
    repository: BillingRetryRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    provider: PaymentProvider,
    clock: Clock,
    billing_key_cipher: BillingKeyCipher,
    idempotency_key: str,
    operation_locks: OperationLockRepository | None = None,
    subscription_billing_uow_factory: (
        SubscriptionBillingUnitOfWorkFactory | None
    ) = None,
) -> BillingRetryResult:
    payload = {
        "invoiceId": invoice_id,
        "force": command.force,
        "reason": command.reason,
        "dryRun": command.dry_run,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key)
    now = clock.utc_now()
    existing_key = await idempotency_keys.find_idempotency_key(
        INTERNAL_BILLING_RETRY_IDEMPOTENCY_SCOPE,
        key_hash,
    )
    if existing_key is not None and existing_key.request_hash != request_hash:
        raise IdempotencyConflictError("idempotency key was used with another payload")
    if existing_key is not None and existing_key.response_body is not None:
        return _result_from_response_body(existing_key.response_body)
    if existing_key is not None and existing_key.status == "processing":
        raise InvalidStateTransitionError("billing retry is processing")

    invoice = await repository.get_invoice(invoice_id)
    if invoice is None:
        raise ResourceNotFoundError("invoice was not found")
    if invoice.status != "issued":
        raise InvalidStateTransitionError("invoice cannot be retried")
    payment = await _latest_retry_payment(repository, invoice)
    if payment is None or payment.status != "failed":
        raise InvalidStateTransitionError("failed payment is required")
    if not command.force:
        if payment.retry_scheduled_at is None:
            raise InvalidStateTransitionError("retry is not scheduled")
        if payment.retry_scheduled_at > now:
            raise InvalidStateTransitionError("retry is not due")
    if invoice.subscription_id is None:
        raise InvalidStateTransitionError("subscription invoice is required")
    subscription = await repository.get_subscription(invoice.subscription_id)
    if subscription is None:
        raise ResourceNotFoundError("subscription was not found")
    if (
        subscription.user_id != invoice.user_id
        or payment.subscription_id != subscription.id
    ):
        raise InvalidStateTransitionError(
            "payment does not belong to invoice subscription"
        )
    if subscription.status != "past_due":
        raise InvalidStateTransitionError("subscription cannot be retried")
    billing_method = await repository.get_default_billing_method(invoice.user_id)
    if billing_method is None:
        raise InvalidStateTransitionError("default billing method is required")
    instrument = await repository.get_payment_instrument(billing_method.instrument_id)
    if instrument is None or instrument.status != "active":
        raise InvalidStateTransitionError("active billing key is required")
    payment_customer = await payment_customers.get_active_payment_customer_for_user(
        invoice.user_id
    )
    if (
        payment_customer is None
        or payment_customer.id != subscription.payment_customer_id
        or payment_customer.id != billing_method.payment_customer_id
        or payment_customer.id != instrument.payment_customer_id
    ):
        raise InvalidStateTransitionError("payment customer does not match")

    next_billing_at = _next_billing_at(subscription.next_billing_at or now)
    previous_failed_attempts = await _count_failed_billing_cycle_attempts(
        repository,
        subscription.id,
        invoice.billing_cycle_key,
    )
    if command.dry_run:
        result = _result(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            status="retryable",
            invoice_status=invoice.status,
            payment_status=payment.status,
            next_billing_date=next_billing_at,
            receipt_url=None,
            amount=payment.amount,
            billing_date=now,
            queued=False,
        )
        await _save_successful_idempotency_response(
            idempotency_keys,
            existing_key,
            key_hash,
            request_hash,
            now,
            result,
        )
        return result

    operation_lock = await acquire_required_operation_lock(
        operation_locks=operation_locks,
        lock_key=f"subscription-retry:{invoice.id}",
        fencing_counter_key="subscription-retry",
        now=now,
        metadata={
            "api": INTERNAL_BILLING_RETRY_IDEMPOTENCY_SCOPE,
            "invoice_id": invoice.id,
            "subscription_id": subscription.id,
        },
    )
    try:
        processing_key = IdempotencyKey(
            id=(
                existing_key.id
                if existing_key is not None
                else IdempotencyKey.generate_id()
            ),
            scope=INTERNAL_BILLING_RETRY_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            status="processing",
            created_at=existing_key.created_at if existing_key is not None else now,
            updated_at=now,
            expires_at=now + timedelta(hours=24),
            resource_type="invoice",
            resource_id=invoice.id,
            locked_until_at=now + timedelta(minutes=5),
        )
        await idempotency_keys.save_idempotency_key(processing_key)

        retry_payment = Payment(
            id=Payment.generate_id(),
            order_id=generate_uuid_id("ord"),
            amount=payment.amount,
            status="ready",
            created_at=now,
            subscription_id=subscription.id,
            billing_cycle_key=invoice.billing_cycle_key,
            payment_customer_id=subscription.payment_customer_id,
            billing_method_id=billing_method.id,
        )
        try:
            charged = await provider.charge_billing_key(
                billing_key=billing_key_cipher.decrypt(instrument.billing_key),
                customer_key=payment_customer.customer_key,
                order_id=retry_payment.order_id,
                amount=retry_payment.amount,
                order_name=f"Subscription retry {invoice.id}",
                idempotency_key=idempotency_key,
            )
            if (
                charged.order_id != retry_payment.order_id
                or charged.amount != retry_payment.amount
            ):
                raise ProviderError(
                    "provider billing charge response does not match request"
                )
        except ProviderError as exc:
            retry_payment.status = "failed"
            final_failure = not exc.retryable or _is_final_retry_failure(
                previous_failed_attempts + 1
            )
            retry_payment.failure = {
                "code": "BILLING_RETRY_FAILED",
                "providerCode": (
                    exc.provider_code or "PROVIDER_BILLING_RETRY_FAILED"
                ),
                "message": str(exc),
                "retryable": exc.retryable and not final_failure,
                "phase": "billing_retry",
                "reason": (
                    "provider_rejected" if exc.provider_code else "provider_error"
                ),
            }
            next_retry_at = None if final_failure else _next_retry_at(now)
            retry_payment.retry_scheduled_at = next_retry_at
            invoice.payment_id = retry_payment.id
            if final_failure:
                subscription.status = "canceled"
                subscription.cancel_at_period_end = False
                subscription.cancel_at = now
                subscription.canceled_at = now
                subscription.access_until = now
                subscription.next_billing_at = None
                result = _result(
                    invoice_id=invoice.id,
                    subscription_id=subscription.id,
                    status=subscription.status,
                    invoice_status=invoice.status,
                    payment_status=retry_payment.status,
                    next_billing_date=None,
                    receipt_url=None,
                    amount=retry_payment.amount,
                    billing_date=now,
                    queued=True,
                    notification_template="subscription_canceled_payment_failed",
                    notification_payload={
                        "failureReason": str(exc),
                        "cancelReason": "payment_retry_exhausted",
                        "canceledAt": now.isoformat(),
                        "subscriptionManageUrl": "/subscriptions/me",
                        "resubscribeUrl": (
                            "/subscriptions/checkout"
                            f"?productCode={subscription.product_code}"
                        ),
                    },
                )
                await _save_retry_documents_and_response(
                    repository=repository,
                    idempotency_keys=idempotency_keys,
                    subscription_billing_uow_factory=(
                        subscription_billing_uow_factory
                    ),
                    payment=retry_payment,
                    invoice=invoice,
                    subscription=subscription,
                    existing_key=processing_key,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    now=clock.utc_now(),
                    result=result,
                )
                return result
            assert next_retry_at is not None
            result = _result(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                status=subscription.status,
                invoice_status=invoice.status,
                payment_status=retry_payment.status,
                next_billing_date=next_retry_at,
                receipt_url=None,
                amount=retry_payment.amount,
                billing_date=now,
                queued=True,
                notification_template="subscription_payment_failed",
                notification_payload={
                    "retryScheduledAt": (
                        next_retry_at.date().isoformat()
                    ),
                    "billingMethodUpdateUrl": "/billing/methods",
                },
            )
            await _save_retry_documents_and_response(
                repository=repository,
                idempotency_keys=idempotency_keys,
                subscription_billing_uow_factory=subscription_billing_uow_factory,
                payment=retry_payment,
                invoice=invoice,
                subscription=None,
                existing_key=processing_key,
                key_hash=key_hash,
                request_hash=request_hash,
                now=clock.utc_now(),
                result=result,
            )
            return result

        retry_payment.status = "paid"
        retry_payment.payment_key = charged.payment_key
        retry_payment.approved_at = charged.approved_at
        retry_payment.receipt_url = charged.receipt_url
        retry_payment.method = charged.method
        retry_payment.method_detail = charged.method_detail
        retry_payment.provider_response_summary = charged.response_summary
        retry_payment.retry_scheduled_at = None
        invoice.status = "paid"
        invoice.receipt_url = charged.receipt_url
        invoice.payment_id = retry_payment.id
        subscription.status = "active"
        subscription.next_billing_at = next_billing_at

        result = _result(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            status=subscription.status,
            invoice_status=invoice.status,
            payment_status=retry_payment.status,
            next_billing_date=subscription.next_billing_at,
            receipt_url=charged.receipt_url,
            amount=retry_payment.amount,
            billing_date=now,
            queued=True,
        )
        await _save_retry_documents_and_response(
            repository=repository,
            idempotency_keys=idempotency_keys,
            subscription_billing_uow_factory=subscription_billing_uow_factory,
            payment=retry_payment,
            invoice=invoice,
            subscription=subscription,
            existing_key=processing_key,
            key_hash=key_hash,
            request_hash=request_hash,
            now=clock.utc_now(),
            result=result,
        )
        return result
    finally:
        await release_operation_lock(
            operation_locks=operation_locks,
            operation_lock=operation_lock,
            released_at=clock.utc_now(),
        )


def _next_billing_at(current: datetime) -> datetime:
    return current + timedelta(days=30)


def _next_retry_at(current: datetime) -> datetime:
    return current + timedelta(days=1)


async def _count_failed_billing_cycle_attempts(
    repository: BillingRetryRepository,
    subscription_id: str,
    billing_cycle_key: str | None,
) -> int:
    if billing_cycle_key is None:
        return 1
    return await repository.count_failed_payments_for_billing_cycle(
        subscription_id,
        billing_cycle_key,
    )


async def _latest_retry_payment(
    repository: BillingRetryRepository,
    invoice: Invoice,
) -> Payment | None:
    if invoice.subscription_id is not None and invoice.billing_cycle_key is not None:
        latest_failed_payment = (
            await repository.get_latest_failed_payment_for_billing_cycle(
                invoice.subscription_id,
                invoice.billing_cycle_key,
            )
        )
        if latest_failed_payment is not None:
            return latest_failed_payment
    return await repository.get_payment(invoice.payment_id)


def _is_final_retry_failure(failed_attempt_count: int) -> bool:
    return failed_attempt_count >= MAX_BILLING_RETRY_FAILED_ATTEMPTS


def _result(
    *,
    invoice_id: str,
    subscription_id: str,
    status: str,
    invoice_status: str,
    payment_status: str,
    next_billing_date: datetime | None,
    receipt_url: str | None,
    amount: int,
    billing_date: datetime,
    queued: bool,
    notification_template: str = "subscription_payment_paid",
    notification_payload: dict[str, object] | None = None,
) -> BillingRetryResult:
    payload = {
        "invoiceId": invoice_id,
        "amount": amount,
        "billingDate": billing_date.date().isoformat(),
        "receiptUrl": receipt_url,
    }
    if notification_payload is not None:
        payload.update(notification_payload)
    return BillingRetryResult(
        invoice_id=invoice_id,
        subscription_id=subscription_id,
        status=status,
        invoice_status=invoice_status,
        payment_status=payment_status,
        next_billing_date=next_billing_date,
        receipt_url=receipt_url,
        notification={
            "template": notification_template,
            "queued": queued,
            "payload": payload,
        },
    )


async def _save_retry_documents_and_response(
    *,
    repository: BillingRetryRepository,
    idempotency_keys: IdempotencyKeyRepository,
    subscription_billing_uow_factory: SubscriptionBillingUnitOfWorkFactory | None,
    payment: Payment,
    invoice: Invoice,
    subscription: Subscription | None,
    existing_key: IdempotencyKey | None,
    key_hash: str,
    request_hash: str,
    now: datetime,
    result: BillingRetryResult,
) -> None:
    if subscription_billing_uow_factory is not None:
        async with subscription_billing_uow_factory() as uow:
            await uow.billing.save_payment(payment)
            await uow.billing.save_invoice(invoice)
            if subscription is not None:
                await uow.billing.save_subscription(subscription)
            await _save_successful_idempotency_response(
                uow.idempotency_keys,
                existing_key,
                key_hash,
                request_hash,
                now,
                result,
            )
        return
    await repository.save_payment(payment)
    await repository.save_invoice(invoice)
    if subscription is not None:
        await repository.save_subscription(subscription)
    await _save_successful_idempotency_response(
        idempotency_keys,
        existing_key,
        key_hash,
        request_hash,
        now,
        result,
    )


async def _save_successful_idempotency_response(
    idempotency_keys: IdempotencyKeyRepository,
    existing_key: IdempotencyKey | None,
    key_hash: str,
    request_hash: str,
    now: datetime,
    result: BillingRetryResult,
) -> None:
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=(
                existing_key.id
                if existing_key is not None
                else IdempotencyKey.generate_id()
            ),
            scope=INTERNAL_BILLING_RETRY_IDEMPOTENCY_SCOPE,
            key_hash=key_hash,
            request_hash=request_hash,
            status="succeeded",
            created_at=existing_key.created_at if existing_key is not None else now,
            updated_at=now,
            expires_at=existing_key.expires_at
            if existing_key is not None
            else now + timedelta(hours=24),
            resource_type="invoice",
            resource_id=result.invoice_id,
            response_status=200,
            response_body=_result_to_response_body(result),
        )
    )


def _result_to_response_body(result: BillingRetryResult) -> dict[str, object]:
    return {
        "invoiceId": result.invoice_id,
        "subscriptionId": result.subscription_id,
        "status": result.status,
        "invoiceStatus": result.invoice_status,
        "paymentStatus": result.payment_status,
        "nextBillingDate": result.next_billing_date,
        "receiptUrl": result.receipt_url,
        "notification": result.notification,
    }


def _result_from_response_body(body: dict[str, object]) -> BillingRetryResult:
    next_billing_date = body["nextBillingDate"]
    if next_billing_date is not None and not isinstance(next_billing_date, datetime):
        raise InvalidStateTransitionError(
            "idempotency response nextBillingDate is invalid"
        )
    return BillingRetryResult(
        invoice_id=str(body["invoiceId"]),
        subscription_id=str(body["subscriptionId"]),
        status=str(body["status"]),
        invoice_status=str(body["invoiceStatus"]),
        payment_status=str(body["paymentStatus"]),
        next_billing_date=next_billing_date,
        receipt_url=(
            str(body["receiptUrl"]) if body["receiptUrl"] is not None else None
        ),
        notification=_dict(body["notification"]),
    )


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidStateTransitionError("idempotency response object is invalid")
    return value


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
