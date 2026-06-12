from __future__ import annotations

from datetime import UTC, datetime

from payments.application.webhooks import receive_toss_payment_webhook
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.webhook_event import WebhookEvent


class FakeWebhookRepository:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], WebhookEvent] = {}
        self.payments: dict[str, Payment] = {}
        self.checkouts: dict[str, Checkout] = {}
        self.one_time_skus: dict[str, OneTimeSku] = {}
        self.invoices: dict[str, Invoice] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.payment_save_count = 0
        self.invoice_save_count = 0
        self.subscription_save_count = 0
        self.operations: list[str] = []

    async def get_webhook_event(
        self,
        provider: str,
        event_id: str,
    ) -> WebhookEvent | None:
        self.operations.append("get_webhook_event")
        return self.events.get((provider, event_id))

    async def get_processed_webhook_event_by_payment_status(
        self,
        *,
        provider: str,
        payment_key: str,
        provider_status: str,
        exclude_event_id: str,
    ) -> WebhookEvent | None:
        self.operations.append("get_processed_webhook_event_by_payment_status")
        return next(
            (
                event
                for event in self.events.values()
                if event.provider == provider
                and event.event_id != exclude_event_id
                and event.payment_key == payment_key
                and event.payload.get("status") == provider_status
                and event.status in {"processed", "ignored"}
            ),
            None,
        )

    async def save_webhook_event(self, event: WebhookEvent) -> None:
        self.operations.append("save_webhook_event")
        self.events[(event.provider, event.event_id)] = event

    async def get_payment_by_order_or_key(
        self,
        *,
        order_id: str | None,
        payment_key: str | None,
    ) -> Payment | None:
        self.operations.append("get_payment_by_order_or_key")
        return next(
            (
                payment
                for payment in self.payments.values()
                if payment.order_id == order_id or payment.payment_key == payment_key
            ),
            None,
        )

    async def save_payment(self, payment: Payment) -> None:
        self.payment_save_count += 1
        self.payments[payment.id] = payment

    async def get_checkout(self, checkout_id: str) -> Checkout | None:
        return self.checkouts.get(checkout_id)

    async def mark_checkout_paid_if_ready(
        self,
        checkout_id: str,
        user_id: str,
        last_payment_id: str,
    ) -> bool:
        checkout = self.checkouts.get(checkout_id)
        if (
            checkout is None
            or checkout.user_id != user_id
            or checkout.status != "ready"
        ):
            return False
        checkout.status = "paid"
        checkout.last_payment_id = last_payment_id
        return True

    async def capture_checkout_reserved_stock(self, checkout: Checkout) -> None:
        for item in checkout.items:
            sku_id = item.get("skuId")
            quantity = item.get("quantity")
            if not isinstance(sku_id, str) or not isinstance(quantity, int):
                continue
            sku = self.one_time_skus.get(sku_id)
            if sku is None or sku.stock_policy != "limited":
                continue
            sku.reserved_stock = max((sku.reserved_stock or 0) - quantity, 0)
            sku.sold_stock = (sku.sold_stock or 0) + quantity

    async def get_invoice_by_payment_id(self, payment_id: str) -> Invoice | None:
        return next(
            (
                invoice
                for invoice in self.invoices.values()
                if invoice.payment_id == payment_id
            ),
            None,
        )

    async def save_invoice(self, invoice: Invoice) -> None:
        self.invoice_save_count += 1
        self.invoices[invoice.id] = invoice

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def save_subscription(self, subscription: Subscription) -> None:
        self.subscription_save_count += 1
        self.subscriptions[subscription.id] = subscription


async def test_receive_toss_webhook_stores_event_and_marks_payment_paid(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_123",
        order_id="ord_123",
        amount=10_000,
        status="ready",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    repository.payments[payment.id] = payment

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_123",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
            "approvedAt": "2026-06-10T00:01:00+00:00",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert result.duplicate is False
    assert payment.status == "paid"
    assert payment.payment_key == "paykey_123"
    assert payment.approved_at == datetime(2026, 6, 10, 0, 1, tzinfo=UTC)
    assert ("tosspayments", "evt_123") in repository.events
    save_event_index = repository.operations.index("save_webhook_event")
    get_payment_index = repository.operations.index("get_payment_by_order_or_key")
    assert save_event_index < get_payment_index


async def test_receive_toss_webhook_reconciles_one_time_checkout_and_stock(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    checkout = Checkout(
        id="chk_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        items=[
            {
                "skuId": "sku_report_pack_100",
                "quantity": 2,
                "unitAmount": 5_000,
                "amount": 10_000,
            }
        ],
        status="ready",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        last_payment_id="pay_123",
    )
    sku = OneTimeSku(
        id="sku_report_pack_100",
        product_id="product_report",
        sku_code="REPORT100",
        amount=5_000,
        currency="KRW",
        status="active",
        stock_policy="limited",
        total_stock=5,
        reserved_stock=2,
        sold_stock=0,
    )
    payment = Payment(
        id="pay_123",
        order_id="ord_123",
        amount=10_000,
        status="ready",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        checkout_id=checkout.id,
    )
    repository.checkouts[checkout.id] = checkout
    repository.one_time_skus[sku.id] = sku
    repository.payments[payment.id] = payment

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_one_time_paid",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
            "totalAmount": 10_000,
            "approvedAt": "2026-06-10T00:01:00+00:00",
            "receiptUrl": "https://dashboard.tosspayments.com/receipt",
        },
        repository,
        fixed_clock,
    )

    invoice = next(iter(repository.invoices.values()))
    assert result.received is True
    assert payment.status == "paid"
    assert checkout.status == "paid"
    assert checkout.last_payment_id == payment.id
    assert sku.reserved_stock == 0
    assert sku.sold_stock == 2
    assert invoice.user_id == "user_1"
    assert invoice.payment_id == payment.id
    assert invoice.status == "paid"
    assert invoice.receipt_url == "https://dashboard.tosspayments.com/receipt"
    assert repository.events[("tosspayments", "evt_one_time_paid")].status == (
        "processed"
    )


async def test_receive_toss_webhook_reconciles_invoice_and_subscription(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    subscription = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
    )
    payment = Payment(
        id="pay_123",
        order_id="ord_123",
        amount=10_000,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    invoice = Invoice(
        id="inv_123",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_123",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
            "approvedAt": "2026-06-10T00:01:00+00:00",
            "receiptUrl": "https://dashboard.tosspayments.com/receipt",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert payment.status == "paid"
    assert invoice.status == "paid"
    assert invoice.receipt_url == "https://dashboard.tosspayments.com/receipt"
    assert subscription.status == "active"
    assert repository.events[("tosspayments", "evt_123")].status == "processed"


async def test_receive_toss_webhook_fails_existing_payment_key_mismatch(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_123",
        order_id="ord_123",
        amount=10_000,
        status="paid",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        payment_key="paykey_original",
    )
    repository.payments[payment.id] = payment

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_key_mismatch",
            "paymentKey": "paykey_tampered",
            "orderId": "ord_123",
            "status": "DONE",
            "approvedAt": "2026-06-10T00:01:00+00:00",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert payment.payment_key == "paykey_original"
    assert payment.status == "paid"
    assert repository.payment_save_count == 0
    assert repository.events[("tosspayments", "evt_key_mismatch")].status == "failed"


async def test_receive_toss_webhook_fails_amount_mismatch(fixed_clock) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_123",
        order_id="ord_123",
        amount=10_000,
        status="ready",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    repository.payments[payment.id] = payment

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_amount_mismatch",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
            "totalAmount": 11_000,
            "approvedAt": "2026-06-10T00:01:00+00:00",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert payment.status == "ready"
    assert payment.payment_key is None
    assert repository.payment_save_count == 0
    assert repository.events[("tosspayments", "evt_amount_mismatch")].status == (
        "failed"
    )


async def test_receive_toss_webhook_ignores_duplicate_event(fixed_clock) -> None:
    repository = FakeWebhookRepository()
    await receive_toss_payment_webhook(
        {"eventId": "evt_123", "orderId": "ord_123", "status": "DONE"},
        repository,
        fixed_clock,
    )

    result = await receive_toss_payment_webhook(
        {"eventId": "evt_123", "orderId": "ord_123", "status": "DONE"},
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert result.duplicate is True


async def test_receive_toss_webhook_ignores_duplicate_payment_key_status(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_123",
        order_id="ord_123",
        amount=10_000,
        status="ready",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    repository.payments[payment.id] = payment
    await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_123",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
            "approvedAt": "2026-06-10T00:01:00+00:00",
        },
        repository,
        fixed_clock,
    )

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_retry_123",
            "paymentKey": "paykey_123",
            "orderId": "ord_123",
            "status": "DONE",
            "approvedAt": "2026-06-10T00:01:00+00:00",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert result.duplicate is True
    assert payment.status == "paid"
    assert repository.payment_save_count == 1
    assert repository.events[("tosspayments", "evt_retry_123")].status == "ignored"


async def test_receive_toss_webhook_reprocesses_unfinished_event(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_unfinished",
        order_id="ord_unfinished",
        amount=10_000,
        status="ready",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    event = WebhookEvent(
        id="wh_unfinished",
        provider="tosspayments",
        event_id="evt_unfinished",
        event_type="PAYMENT_STATUS_CHANGED",
        payment_key="paykey_unfinished",
        order_id=payment.order_id,
        status="received",
        payload={
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_unfinished",
            "paymentKey": "paykey_unfinished",
            "orderId": payment.order_id,
            "status": "DONE",
            "approvedAt": "2026-06-10T00:01:00+00:00",
        },
        received_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    repository.payments[payment.id] = payment
    repository.events[(event.provider, event.event_id)] = event

    result = await receive_toss_payment_webhook(
        event.payload,
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert result.duplicate is False
    assert payment.status == "paid"
    assert repository.events[("tosspayments", "evt_unfinished")].status == "processed"


async def test_receive_toss_webhook_ignores_already_paid_plan_change(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    subscription = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_pro_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
    )
    payment = Payment(
        id="pay_plan_change",
        order_id="ord_plan_change",
        amount=5_000,
        status="paid",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        payment_key="paykey_plan_change",
        approved_at=datetime(2026, 6, 10, 0, 1, tzinfo=UTC),
        receipt_url="https://dashboard.tosspayments.com/receipt/plan-change",
        cancelable_amount=5_000,
        provider_response_summary={
            "provider": "tosspayments",
            "providerStatus": "DONE",
            "paymentKey": "paykey_plan_change",
            "orderId": "ord_plan_change",
        },
    )
    invoice = Invoice(
        id="inv_plan_change",
        user_id="user_1",
        payment_id=payment.id,
        status="paid",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        receipt_url=payment.receipt_url,
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_plan_change_late",
            "paymentKey": payment.payment_key,
            "orderId": payment.order_id,
            "status": "DONE",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert payment.status == "paid"
    assert payment.approved_at == datetime(2026, 6, 10, 0, 1, tzinfo=UTC)
    assert invoice.status == "paid"
    assert subscription.plan_id == "plan_pro_monthly"
    assert repository.payment_save_count == 0
    assert repository.invoice_save_count == 0
    assert repository.subscription_save_count == 0
    assert repository.events[("tosspayments", "evt_plan_change_late")].status == (
        "ignored"
    )


async def test_receive_toss_webhook_ignores_older_provider_status_event(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_old_event",
        order_id="ord_old_event",
        amount=10_000,
        status="paid",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        payment_key="paykey_old_event",
        approved_at=datetime(2026, 6, 10, 0, 10, tzinfo=UTC),
        cancelable_amount=10_000,
        provider_response_summary={
            "provider": "tosspayments",
            "providerStatus": "DONE",
            "paymentKey": "paykey_old_event",
            "orderId": "ord_old_event",
            "approvedAt": datetime(2026, 6, 10, 0, 10, tzinfo=UTC),
        },
    )
    invoice = Invoice(
        id="inv_old_event",
        user_id="user_1",
        payment_id=payment.id,
        status="paid",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_old_cancel",
            "paymentKey": payment.payment_key,
            "orderId": payment.order_id,
            "status": "CANCELED",
            "canceledAt": "2026-06-10T00:09:59+00:00",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert payment.status == "paid"
    assert invoice.status == "paid"
    assert repository.payment_save_count == 0
    assert repository.invoice_save_count == 0
    assert repository.events[("tosspayments", "evt_old_cancel")].status == "ignored"


async def test_receive_toss_webhook_processes_newer_same_status_after_ignored_event(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_new_cancel",
        order_id="ord_new_cancel",
        amount=10_000,
        status="paid",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        payment_key="paykey_new_cancel",
        approved_at=datetime(2026, 6, 10, 0, 10, tzinfo=UTC),
        receipt_url="https://dashboard.tosspayments.com/receipt",
        cancelable_amount=10_000,
        provider_response_summary={
            "provider": "tosspayments",
            "providerStatus": "DONE",
            "paymentKey": "paykey_new_cancel",
            "orderId": "ord_new_cancel",
            "approvedAt": datetime(2026, 6, 10, 0, 10, tzinfo=UTC),
        },
    )
    ignored_event = WebhookEvent(
        id="wh_old_cancel",
        provider="tosspayments",
        event_id="evt_old_cancel",
        event_type="PAYMENT_STATUS_CHANGED",
        payment_key=payment.payment_key,
        order_id=payment.order_id,
        status="ignored",
        payload={
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_old_cancel",
            "paymentKey": payment.payment_key,
            "orderId": payment.order_id,
            "status": "CANCELED",
            "canceledAt": "2026-06-10T00:09:59+00:00",
        },
    )
    invoice = Invoice(
        id="inv_new_cancel",
        user_id="user_1",
        payment_id=payment.id,
        status="paid",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        receipt_url=payment.receipt_url,
    )
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice
    repository.events[(ignored_event.provider, ignored_event.event_id)] = ignored_event

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_new_cancel",
            "paymentKey": payment.payment_key,
            "orderId": payment.order_id,
            "status": "CANCELED",
            "canceledAt": "2026-06-10T00:12:00+00:00",
            "statusChangedAt": "2026-06-10T00:12:00+00:00",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert result.duplicate is False
    assert payment.status == "canceled"
    assert invoice.status == "refunded"
    assert payment.provider_response_summary == {
        "provider": "tosspayments",
        "providerStatus": "CANCELED",
        "paymentKey": payment.payment_key,
        "orderId": payment.order_id,
        "statusChangedAt": datetime(2026, 6, 10, 0, 12, tzinfo=UTC),
        "canceledAt": datetime(2026, 6, 10, 0, 12, tzinfo=UTC),
    }
    assert repository.events[("tosspayments", "evt_new_cancel")].status == "processed"


async def test_receive_toss_webhook_keeps_cancel_summary_after_late_done_event(
    fixed_clock,
) -> None:
    repository = FakeWebhookRepository()
    payment = Payment(
        id="pay_late_done",
        order_id="ord_late_done",
        amount=10_000,
        status="paid",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        payment_key="paykey_late_done",
        approved_at=datetime(2026, 6, 10, 0, 10, tzinfo=UTC),
        receipt_url="https://dashboard.tosspayments.com/receipt",
        cancelable_amount=10_000,
        provider_response_summary={
            "provider": "tosspayments",
            "providerStatus": "DONE",
            "paymentKey": "paykey_late_done",
            "orderId": "ord_late_done",
            "approvedAt": datetime(2026, 6, 10, 0, 10, tzinfo=UTC),
        },
    )
    invoice = Invoice(
        id="inv_late_done",
        user_id="user_1",
        payment_id=payment.id,
        status="paid",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        receipt_url=payment.receipt_url,
    )
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice

    await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_cancel_before_done",
            "paymentKey": payment.payment_key,
            "orderId": payment.order_id,
            "status": "CANCELED",
            "approvedAt": "2026-06-10T00:10:00+00:00",
            "canceledAt": "2026-06-10T00:12:00+00:00",
            "statusChangedAt": "2026-06-10T00:12:00+00:00",
        },
        repository,
        fixed_clock,
    )
    summary_after_cancel = payment.provider_response_summary

    result = await receive_toss_payment_webhook(
        {
            "eventType": "PAYMENT_STATUS_CHANGED",
            "eventId": "evt_late_done",
            "paymentKey": payment.payment_key,
            "orderId": payment.order_id,
            "status": "DONE",
            "approvedAt": "2026-06-10T00:10:00+00:00",
        },
        repository,
        fixed_clock,
    )

    assert result.received is True
    assert payment.status == "canceled"
    assert invoice.status == "refunded"
    assert payment.provider_response_summary == summary_after_cancel
    assert repository.events[("tosspayments", "evt_late_done")].status == "ignored"
