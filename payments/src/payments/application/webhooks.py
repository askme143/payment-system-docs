from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from payments.application.ports.clock import Clock
from payments.application.ports.unit_of_work import WebhookUnitOfWorkFactory
from payments.application.ports.webhooks import WebhookRepository
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.webhook_event import WebhookEvent


@dataclass(frozen=True, slots=True)
class TossPaymentWebhookResult:
    received: bool
    duplicate: bool = False


async def receive_toss_payment_webhook(
    payload: dict[str, object],
    repository: WebhookRepository,
    clock: Clock,
    webhook_uow_factory: WebhookUnitOfWorkFactory | None = None,
) -> TossPaymentWebhookResult:
    """토스 결제 상태 웹훅을 저장하고 내부 결제 상태를 보정합니다."""
    event_id = _string(payload.get("eventId")) or _fallback_event_id(payload)
    existing = await repository.get_webhook_event("tosspayments", event_id)
    if existing is not None and existing.status in {"processed", "ignored"}:
        return TossPaymentWebhookResult(received=True, duplicate=True)

    now = clock.utc_now()
    event = existing or WebhookEvent(
        id=WebhookEvent.generate_id(),
        provider="tosspayments",
        event_id=event_id,
        event_type=_string(payload.get("eventType")) or "PAYMENT_STATUS_CHANGED",
        payment_key=_string(payload.get("paymentKey")),
        order_id=_string(payload.get("orderId")),
        status="received",
        payload=payload,
        received_at=now,
    )
    if existing is None:
        await repository.save_webhook_event(event)
    payload = event.payload

    if webhook_uow_factory is not None:
        async with webhook_uow_factory() as uow:
            return await _process_toss_payment_webhook_event(
                event=event,
                payload=payload,
                repository=uow.webhooks,
                processed_at=now,
            )
    return await _process_toss_payment_webhook_event(
        event=event,
        payload=payload,
        repository=repository,
        processed_at=now,
    )


async def _process_toss_payment_webhook_event(
    *,
    event: WebhookEvent,
    payload: dict[str, object],
    repository: WebhookRepository,
    processed_at: datetime,
) -> TossPaymentWebhookResult:
    duplicate_status_event = (
        await _get_processed_duplicate_payment_status_event(
            repository=repository,
            event=event,
            payload=payload,
        )
    )
    if duplicate_status_event is not None:
        event.status = "ignored"
        event.processed_at = processed_at
        await repository.save_webhook_event(event)
        return TossPaymentWebhookResult(received=True, duplicate=True)
    payment = await repository.get_payment_by_order_or_key(
        order_id=event.order_id,
        payment_key=event.payment_key,
    )
    if payment is not None:
        if _webhook_payment_mismatches(payment, event, payload):
            event.status = "failed"
            event.processed_at = processed_at
            await repository.save_webhook_event(event)
            return TossPaymentWebhookResult(received=True)
        if _webhook_event_is_older(payment, payload):
            event.status = "ignored"
            event.processed_at = processed_at
            await repository.save_webhook_event(event)
            return TossPaymentWebhookResult(received=True)
        provider_status = _string(payload.get("status"))
        checkout = None
        checkout_marked_paid = False
        if provider_status == "DONE" and payment.checkout_id is not None:
            checkout = await repository.get_checkout(payment.checkout_id)
            checkout_was_paid = checkout is not None and checkout.status == "paid"
            if checkout is None or not await _claim_one_time_checkout_paid(
                repository=repository,
                checkout=checkout,
                payment=payment,
            ):
                event.status = "failed"
                event.processed_at = processed_at
                await repository.save_webhook_event(event)
                return TossPaymentWebhookResult(received=True)
            checkout_marked_paid = not checkout_was_paid
        changed = False
        payment_state = _payment_state(payment)
        _apply_payment_status(payment, event, payload)
        if _payment_state(payment) != payment_state:
            changed = True
            await repository.save_payment(payment)
        invoice = await repository.get_invoice_by_payment_id(payment.id)
        if invoice is None and checkout is not None and payment.status == "paid":
            invoice = Invoice(
                id=Invoice.generate_id(),
                user_id=checkout.user_id,
                payment_id=payment.id,
                status="paid",
                issued_at=payment.approved_at or processed_at,
                receipt_url=payment.receipt_url,
            )
            changed = True
            await repository.save_invoice(invoice)
        if checkout_marked_paid and checkout is not None and payment.status == "paid":
            await repository.capture_checkout_reserved_stock(checkout)
            changed = True
        if invoice is not None:
            invoice_state = _invoice_state(invoice)
            _apply_invoice_status(invoice, payment)
            if _invoice_state(invoice) != invoice_state:
                changed = True
                await repository.save_invoice(invoice)
        if payment.subscription_id is not None:
            subscription = await repository.get_subscription(payment.subscription_id)
            if subscription is not None:
                subscription_state = _subscription_state(subscription)
                _apply_subscription_status(subscription, payment)
                if _subscription_state(subscription) != subscription_state:
                    changed = True
                    await repository.save_subscription(subscription)
        event.status = "processed" if changed else "ignored"
        event.processed_at = processed_at
        await repository.save_webhook_event(event)
        return TossPaymentWebhookResult(received=True)
    event.status = "ignored"
    event.processed_at = processed_at
    await repository.save_webhook_event(event)
    return TossPaymentWebhookResult(received=True)


async def _get_processed_duplicate_payment_status_event(
    *,
    repository: WebhookRepository,
    event: WebhookEvent,
    payload: dict[str, object],
) -> WebhookEvent | None:
    provider_status = _string(payload.get("status"))
    if event.payment_key is None or provider_status is None:
        return None
    duplicate = await repository.get_processed_webhook_event_by_payment_status(
        provider=event.provider,
        payment_key=event.payment_key,
        provider_status=provider_status,
        exclude_event_id=event.event_id,
    )
    if duplicate is None:
        return None
    if _duplicate_status_event_is_older(duplicate.payload, payload):
        return None
    return duplicate


def _duplicate_status_event_is_older(
    duplicate_payload: dict[str, object],
    payload: dict[str, object],
) -> bool:
    duplicate_timestamp = _provider_status_timestamp(duplicate_payload)
    event_timestamp = _provider_status_timestamp(payload)
    return (
        duplicate_timestamp is not None
        and event_timestamp is not None
        and duplicate_timestamp < event_timestamp
    )


async def _claim_one_time_checkout_paid(
    *,
    repository: WebhookRepository,
    checkout: Checkout,
    payment: Payment,
) -> bool:
    if checkout.status == "paid":
        return checkout.last_payment_id == payment.id
    marked = await repository.mark_checkout_paid_if_ready(
        checkout.id,
        checkout.user_id,
        payment.id,
    )
    if marked:
        checkout.status = "paid"
        checkout.last_payment_id = payment.id
    return marked


def _apply_payment_status(
    payment: Payment,
    event: WebhookEvent,
    payload: dict[str, object],
) -> None:
    provider_status = _string(payload.get("status"))
    if event.payment_key:
        payment.payment_key = event.payment_key
    if provider_status == "DONE":
        if payment.status not in {"paid", "canceled", "partial_canceled"}:
            payment.status = "paid"
        approved_at = _datetime(payload.get("approvedAt"))
        if approved_at is not None:
            payment.approved_at = approved_at
        payment.receipt_url = _string(payload.get("receiptUrl")) or payment.receipt_url
        payment.method = _string(payload.get("method")) or payment.method
        payment.cancelable_amount = payment.amount
        payment.provider_response_summary = _provider_summary(
            event,
            provider_status,
            payload,
        )
        return
    mapped_status = _mapped_cancel_status(provider_status)
    if mapped_status is not None and payment.status != "canceled":
        payment.status = mapped_status
        payment.provider_response_summary = _provider_summary(
            event,
            provider_status,
            payload,
        )


def _apply_invoice_status(invoice: Invoice, payment: Payment) -> None:
    if payment.status == "paid" and invoice.status == "issued":
        invoice.status = "paid"
        invoice.receipt_url = payment.receipt_url or invoice.receipt_url
        return
    if payment.status == "canceled" and invoice.status in {"issued", "paid"}:
        invoice.status = "refunded"


def _apply_subscription_status(subscription: Subscription, payment: Payment) -> None:
    if payment.status == "paid" and subscription.status in {"pending", "past_due"}:
        subscription.status = "active"
        return
    if payment.status == "failed" and subscription.status == "active":
        subscription.status = "past_due"


def _mapped_cancel_status(
    status: str | None,
) -> Literal["canceled", "partial_canceled"] | None:
    if status == "CANCELED":
        return "canceled"
    if status == "PARTIAL_CANCELED":
        return "partial_canceled"
    return None


def _payment_state(payment: Payment) -> tuple[object, ...]:
    return (
        payment.status,
        payment.payment_key,
        payment.approved_at,
        payment.receipt_url,
        payment.method,
        payment.cancelable_amount,
        _stable_dict(payment.provider_response_summary),
    )


def _invoice_state(invoice: Invoice) -> tuple[object, ...]:
    return (
        invoice.status,
        invoice.receipt_url,
    )


def _subscription_state(subscription: Subscription) -> tuple[object, ...]:
    return (
        subscription.status,
        subscription.plan_id,
        subscription.pending_plan_id,
        subscription.pending_plan_effective_at,
        subscription.next_billing_at,
    )


def _stable_dict(value: dict[str, Any] | None) -> tuple[tuple[str, object], ...] | None:
    if value is None:
        return None
    return tuple(sorted(value.items()))


def _provider_summary(
    event: WebhookEvent,
    provider_status: str | None,
    payload: dict[str, object],
) -> dict[str, object]:
    summary: dict[str, object] = {
        "provider": event.provider,
        "providerStatus": provider_status,
        "paymentKey": event.payment_key,
        "orderId": event.order_id,
    }
    total_amount = _payload_amount(payload)
    if total_amount is not None:
        summary["totalAmount"] = total_amount
    approved_at = _datetime(payload.get("approvedAt"))
    if approved_at is not None:
        summary["approvedAt"] = approved_at
    status_changed_at = _datetime(payload.get("statusChangedAt"))
    if status_changed_at is not None:
        summary["statusChangedAt"] = status_changed_at
    canceled_at = _datetime(payload.get("canceledAt"))
    if canceled_at is not None:
        summary["canceledAt"] = canceled_at
    receipt_url = _string(payload.get("receiptUrl"))
    if receipt_url is not None:
        summary["receiptUrl"] = receipt_url
    method = _string(payload.get("method"))
    if method is not None:
        summary["method"] = method
    return summary


def _webhook_payment_mismatches(
    payment: Payment,
    event: WebhookEvent,
    payload: dict[str, object],
) -> bool:
    if event.order_id is not None and payment.order_id != event.order_id:
        return True
    if (
        event.payment_key is not None
        and payment.payment_key is not None
        and payment.payment_key != event.payment_key
    ):
        return True
    total_amount = _payload_amount(payload)
    return total_amount is not None and total_amount != payment.amount


def _webhook_event_is_older(
    payment: Payment,
    payload: dict[str, object],
) -> bool:
    event_timestamp = _provider_status_timestamp(payload)
    current_timestamp = _payment_provider_status_timestamp(payment)
    return (
        event_timestamp is not None
        and current_timestamp is not None
        and event_timestamp < current_timestamp
    )


def _provider_status_timestamp(payload: dict[str, object]) -> datetime | None:
    status = _string(payload.get("status"))
    keys: tuple[str, ...]
    if status in {"CANCELED", "PARTIAL_CANCELED"}:
        keys = ("statusChangedAt", "canceledAt", "approvedAt")
    else:
        keys = ("statusChangedAt", "approvedAt", "canceledAt")
    return _first_datetime(payload, keys)


def _payment_provider_status_timestamp(payment: Payment) -> datetime | None:
    if payment.status in {"canceled", "partial_canceled"}:
        latest_cancel = _latest_cancel_timestamp(payment.cancel_history)
        if latest_cancel is not None:
            return latest_cancel
    summary = payment.provider_response_summary or {}
    summary_timestamp = _first_datetime(
        summary,
        ("statusChangedAt", "canceledAt", "approvedAt"),
    )
    return summary_timestamp or payment.approved_at


def _latest_cancel_timestamp(
    cancel_history: list[dict[str, Any]] | None,
) -> datetime | None:
    timestamps = [
        canceled_at
        for cancel in cancel_history or []
        if (canceled_at := _datetime(cancel.get("canceledAt"))) is not None
    ]
    if not timestamps:
        return None
    return max(timestamps)


def _first_datetime(
    source: dict[str, object],
    keys: tuple[str, ...],
) -> datetime | None:
    for key in keys:
        value = _datetime(source.get(key))
        if value is not None:
            return value
    return None


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _payload_amount(payload: dict[str, object]) -> int | None:
    total_amount = _int(payload.get("totalAmount"))
    if total_amount is not None:
        return total_amount
    return _int(payload.get("amount"))


def _fallback_event_id(payload: dict[str, object]) -> str:
    payment_key = _string(payload.get("paymentKey")) or "unknown"
    status = _string(payload.get("status")) or "unknown"
    return f"{payment_key}:{status}"
