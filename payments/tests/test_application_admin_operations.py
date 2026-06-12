from __future__ import annotations

from datetime import UTC, datetime

import pytest

from payments.application.admin_catalog import AdminRequestContext
from payments.application.admin_operations import (
    ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE,
    AdminListQuery,
    AdminPaymentCancelCommand,
    AdminSubscriptionAdjustCommand,
    adjust_admin_subscription,
    cancel_admin_payment,
    list_admin_payments,
    list_admin_subscriptions,
)
from payments.application.context import RequestContext
from payments.application.errors import (
    BadRequestError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ProviderError,
)
from payments.application.payment_orders import (
    PaymentConfirmCommand,
    PaymentOrderItem,
    PaymentOrderResult,
    confirm_payment,
    create_payment_order,
)
from payments.application.ports import (
    AdminPaymentListRecord,
    AdminSubscriptionListRecord,
    PaymentCancelProviderResult,
    PaymentLookupProviderResult,
)
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.operation_lock import OperationLock
from payments.domain.entities.payment import Payment
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class FakeAdminOperationsRepository:
    def __init__(self) -> None:
        self.subscriptions: dict[str, Subscription] = {}
        self.subscription_plans: dict[str, SubscriptionPlan] = {
            "plan_basic_monthly": SubscriptionPlan(
                id="plan_basic_monthly",
                product_id="product_basic",
                plan_code="basic_monthly",
                billing_period="monthly",
                amount=9900,
                entitlements={"seats": 1},
                status="active",
            )
        }
        self.payments: dict[str, Payment] = {}
        self.invoices: dict[str, Invoice] = {}
        self.audit_records: list[dict[str, object]] = []
        self.payment_records = [
            AdminPaymentListRecord(
                payment_id="pay_123",
                checkout_id="chk_123",
                user_id="user_1",
                user_email="customer@example.com",
                order_id="order_123",
                order_name="Report pack",
                payment_key="paykey_123",
                status="paid",
                amount=25000,
                paid_amount=25000,
                cancelable_amount=25000,
                currency="KRW",
                created_at=datetime(2026, 6, 8, 1, 15, tzinfo=UTC),
                approved_at=datetime(2026, 6, 8, 1, 15, tzinfo=UTC),
                method_summary="card 1234",
            )
        ]
        self.subscription_records = [
            AdminSubscriptionListRecord(
                subscription_id="sub_123",
                user_id="user_1",
                user_email="customer@example.com",
                product_code="analytics",
                product_name="Analytics",
                plan_id="plan_basic",
                plan_name="Basic monthly",
                status="past_due",
                current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
                current_period_end_at=datetime(2026, 6, 30, tzinfo=UTC),
                next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
                payment_failure={
                    "hasFailure": True,
                    "lastInvoiceId": "inv_123",
                    "failureCode": "CARD_DECLINED",
                    "retryScheduledAt": None,
                },
                default_billing_method_summary="card 1234",
            )
        ]

    async def list_admin_payments(
        self,
        query: AdminListQuery,
    ) -> list[AdminPaymentListRecord]:
        return self.payment_records[: query.limit]

    async def list_admin_subscriptions(
        self,
        query: AdminListQuery,
    ) -> list[AdminSubscriptionListRecord]:
        return self.subscription_records[: query.limit]

    async def save_admin_list_audit_record(
        self,
        *,
        audit_id: str,
        admin_id: str,
        request_id: str,
        action: str,
        target_type: str,
        target_id: str,
        query: dict[str, object],
        result_count: int,
        has_more: bool,
        request_ip: str | None,
        created_at: datetime,
    ) -> None:
        self.audit_records.append(
            {
                "audit_id": audit_id,
                "admin_id": admin_id,
                "request_id": request_id,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "query": query,
                "result_count": result_count,
                "has_more": has_more,
                "request_ip": request_ip,
                "created_at": created_at,
            }
        )

    async def get_admin_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def get_admin_subscription_plan(
        self,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        return self.subscription_plans.get(plan_id)

    async def get_admin_payment_by_invoice_id(
        self,
        invoice_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        invoice = self.invoices.get(invoice_id)
        if invoice is None:
            return (None, None)
        return (self.payments.get(invoice.payment_id), invoice)

    async def get_admin_payment_by_payment_key(
        self,
        payment_key: str,
    ) -> Payment | None:
        return next(
            (
                payment
                for payment in self.payments.values()
                if payment.payment_key == payment_key
            ),
            None,
        )

    async def get_admin_invoice_by_payment_id(
        self,
        payment_id: str,
    ) -> Invoice | None:
        return next(
            (
                invoice
                for invoice in self.invoices.values()
                if invoice.payment_id == payment_id
            ),
            None,
        )

    async def get_admin_latest_failed_subscription_payment(
        self,
        subscription_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        payments = [
            payment
            for payment in self.payments.values()
            if payment.subscription_id == subscription_id and payment.status == "failed"
        ]
        if not payments:
            return (None, None)
        payment = max(payments, key=lambda item: item.created_at)
        return (payment, await self.get_admin_invoice_by_payment_id(payment.id))

    async def save_admin_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription

    async def save_admin_payment(self, payment: Payment) -> None:
        self.payments[payment.id] = payment

    async def save_admin_invoice(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice

    async def save_subscription_adjustment_audit_record(
        self,
        *,
        audit_id: str,
        subscription_id: str,
        admin_id: str,
        request_id: str,
        adjustment_type: str,
        reason_code: str,
        reason_message: str,
        previous: dict[str, object],
        next_value: dict[str, object],
        notified_customer: bool,
        request_ip: str | None = None,
        result: str = "succeeded",
        idempotency_key_id: str | None = None,
        idempotency_scope: str | None = None,
        idempotency_key_hash: str | None = None,
        idempotency_request_hash: str | None = None,
    ) -> None:
        self.audit_records.append(
            {
                "audit_id": audit_id,
                "subscription_id": subscription_id,
                "admin_id": admin_id,
                "request_id": request_id,
                "adjustment_type": adjustment_type,
                "reason_code": reason_code,
                "reason_message": reason_message,
                "previous": previous,
                "next": next_value,
                "notified_customer": notified_customer,
                "request_ip": request_ip,
                "result": result,
                "idempotency_key_id": idempotency_key_id,
                "idempotency_scope": idempotency_scope,
                "idempotency_key_hash": idempotency_key_hash,
                "idempotency_request_hash": idempotency_request_hash,
            }
        )


class MutatingOperationLockRepository:
    def __init__(
        self,
        *,
        repository: FakeAdminOperationsRepository,
        replacement_subscription: Subscription,
    ) -> None:
        self._repository = repository
        self._replacement_subscription = replacement_subscription
        self.acquire_calls: list[str] = []
        self.release_calls: list[str] = []

    async def acquire_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        fencing_counter_key: str,
        locked_until_at: datetime,
        acquired_at: datetime,
        metadata: dict[str, object] | None = None,
    ) -> OperationLock | None:
        self.acquire_calls.append(lock_key)
        self._repository.subscriptions[self._replacement_subscription.id] = (
            self._replacement_subscription
        )
        return OperationLock(
            id=OperationLock.generate_id(),
            lock_key=lock_key,
            owner_token=owner_token,
            fencing_token=1,
            fencing_counter_key=fencing_counter_key,
            status="active",
            locked_until_at=locked_until_at,
            acquired_at=acquired_at,
            metadata=metadata,
        )

    async def release_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        released_at: datetime,
    ) -> None:
        self.release_calls.append(lock_key)


async def create_confirmed_admin_cancel_payment(
    test_dependencies,
    *,
    request_id: str,
    confirm_request_id: str,
    payment_key: str,
    confirm_key: str,
) -> PaymentOrderResult:
    order = await create_payment_order(
        requester=RequestContext(request_id=request_id, user_id="user_1"),
        items=[PaymentOrderItem(sku_id="sku_report_pack_100", quantity=2)],
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id=confirm_request_id, user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=order.payment_id,
            payment_key=payment_key,
            order_id=order.order_id,
            amount=order.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key=confirm_key,
    )
    return order


async def test_list_admin_payments_returns_page() -> None:
    result = await list_admin_payments(
        AdminListQuery(limit=50),
        FakeAdminOperationsRepository(),
    )

    assert result.items[0].payment_id == "pay_123"
    assert result.items[0].cancel_url == "/admin/payments/pay_123/cancel"
    assert result.page.has_more is False


async def test_list_admin_payments_records_search_audit(test_dependencies) -> None:
    repository = FakeAdminOperationsRepository()

    await list_admin_payments(
        AdminListQuery(
            status=("paid", "failed"),
            user_id="user_1",
            order_id="order_123",
            payment_key="paykey_123",
            limit=50,
        ),
        repository,
        AdminRequestContext(
            request_id="req_admin_payment_list",
            admin_id="admin_1",
            request_ip="203.0.113.10",
        ),
        test_dependencies.clock,
    )

    audit = repository.audit_records[0]
    assert audit["action"] == "payment.list"
    assert audit["target_type"] == "payment"
    assert audit["target_id"] == "admin-payments"
    assert audit["admin_id"] == "admin_1"
    assert audit["request_id"] == "req_admin_payment_list"
    assert audit["request_ip"] == "203.0.113.10"
    assert audit["query"] == {
        "limit": 50,
        "status": ["paid", "failed"],
        "userId": "user_1",
        "orderId": "order_123",
        "paymentKey": "paykey_123",
    }
    assert audit["result_count"] == 1
    assert audit["has_more"] is False


async def test_list_admin_payments_returns_cursor_for_unapproved_payment() -> None:
    repository = FakeAdminOperationsRepository()
    repository.payment_records = [
        AdminPaymentListRecord(
            payment_id="pay_ready",
            checkout_id="chk_ready",
            user_id="user_1",
            user_email="customer@example.com",
            order_id="order_ready",
            order_name="Ready order",
            payment_key=None,
            status="ready",
            amount=25000,
            paid_amount=0,
            cancelable_amount=0,
            currency="KRW",
            created_at=datetime(2026, 6, 9, tzinfo=UTC),
            approved_at=None,
            method_summary=None,
        ),
        AdminPaymentListRecord(
            payment_id="pay_old",
            checkout_id="chk_old",
            user_id="user_1",
            user_email="customer@example.com",
            order_id="order_old",
            order_name="Old order",
            payment_key=None,
            status="failed",
            amount=25000,
            paid_amount=0,
            cancelable_amount=0,
            currency="KRW",
            created_at=datetime(2026, 6, 8, tzinfo=UTC),
            approved_at=None,
            method_summary=None,
        ),
    ]

    result = await list_admin_payments(AdminListQuery(limit=1), repository)

    assert result.items[0].payment_id == "pay_ready"
    assert result.page.has_more is True
    assert result.page.next_cursor is not None


async def test_list_admin_subscriptions_returns_page() -> None:
    result = await list_admin_subscriptions(
        AdminListQuery(limit=50),
        FakeAdminOperationsRepository(),
    )

    assert result.items[0].subscription_id == "sub_123"
    assert result.items[0].adjust_url == "/admin/subscriptions/sub_123/adjust"
    assert result.page.next_cursor is None


async def test_list_admin_subscriptions_records_search_audit(test_dependencies) -> None:
    repository = FakeAdminOperationsRepository()

    await list_admin_subscriptions(
        AdminListQuery(
            status="past_due",
            user_id="user_1",
            product_code="analytics",
            payment_failure=True,
            next_billing_from=datetime(2026, 7, 1, tzinfo=UTC),
            next_billing_to=datetime(2026, 7, 31, tzinfo=UTC),
            limit=25,
        ),
        repository,
        AdminRequestContext(
            request_id="req_admin_subscription_list",
            admin_id="admin_1",
            request_ip="203.0.113.11",
        ),
        test_dependencies.clock,
    )

    audit = repository.audit_records[0]
    assert audit["action"] == "subscription.list"
    assert audit["target_type"] == "subscription"
    assert audit["target_id"] == "admin-subscriptions"
    assert audit["admin_id"] == "admin_1"
    assert audit["request_id"] == "req_admin_subscription_list"
    assert audit["request_ip"] == "203.0.113.11"
    assert audit["query"] == {
        "limit": 25,
        "status": ["past_due"],
        "userId": "user_1",
        "productCode": "analytics",
        "paymentFailure": True,
        "nextBillingFrom": datetime(2026, 7, 1, tzinfo=UTC),
        "nextBillingTo": datetime(2026, 7, 31, tzinfo=UTC),
    }
    assert audit["result_count"] == 1
    assert audit["has_more"] is False


async def test_list_admin_subscriptions_returns_cursor_for_null_next_billing() -> None:
    repository = FakeAdminOperationsRepository()
    repository.subscription_records = [
        AdminSubscriptionListRecord(
            subscription_id="sub_canceled_1",
            user_id="user_1",
            user_email="customer@example.com",
            product_code="analytics",
            product_name="Analytics",
            plan_id="plan_basic",
            plan_name="Basic monthly",
            status="canceled",
            current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 30, tzinfo=UTC),
            next_billing_at=None,
            payment_failure={"hasFailure": False},
            default_billing_method_summary="card 1234",
        ),
        AdminSubscriptionListRecord(
            subscription_id="sub_canceled_2",
            user_id="user_1",
            user_email="customer@example.com",
            product_code="analytics",
            product_name="Analytics",
            plan_id="plan_basic",
            plan_name="Basic monthly",
            status="canceled",
            current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 30, tzinfo=UTC),
            next_billing_at=None,
            payment_failure={"hasFailure": False},
            default_billing_method_summary="card 1234",
        ),
    ]

    result = await list_admin_subscriptions(AdminListQuery(limit=1), repository)

    assert result.items[0].subscription_id == "sub_canceled_1"
    assert result.page.has_more is True
    assert result.page.next_cursor is not None


async def test_list_admin_subscriptions_rejects_pending_status_filter() -> None:
    with pytest.raises(BadRequestError):
        await list_admin_subscriptions(
            AdminListQuery(status="pending", limit=50),
            FakeAdminOperationsRepository(),
        )


async def test_cancel_admin_payment_records_admin_cancel(test_dependencies) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    order = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=[PaymentOrderItem(sku_id="sku_report_pack_100", quantity=2)],
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=order.payment_id,
            payment_key="paykey_admin",
            order_id=order.order_id,
            amount=order.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-admin-key",
    )

    result = await cancel_admin_payment(
        AdminRequestContext(
            request_id="req_admin",
            admin_id="admin_1",
            request_ip="203.0.113.10",
        ),
        order.payment_id,
        AdminPaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="duplicate_payment",
            reason_message="duplicate order",
            notify_customer=True,
        ),
        test_dependencies.one_time_payment_uow_factory,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        idempotency_key="admin-cancel-key",
    )

    payment = test_dependencies.payment_stores.payments.payments[order.payment_id]
    assert result.payment_id == order.payment_id
    assert result.status == "canceled"
    assert result.operator_audit_id.startswith("audit_")
    assert isinstance(result.cancel_history[0]["cancelId"], str)
    assert result.cancel_history[0]["cancelId"].startswith("pcancel_")
    assert result.cancel_history[0]["providerCancelId"] == "cnl_123"
    assert result.cancel_history[0]["requestedBy"] == "admin"
    assert result.cancel_history[0]["adminId"] == "admin_1"
    assert test_dependencies.payment_provider.last_cancel_payment_idempotency_key == (
        "admin-cancel-key"
    )
    assert payment.cancel_history == result.cancel_history
    assert sku.reserved_stock == 0
    assert sku.sold_stock == 0
    cancel_request = next(
        iter(test_dependencies.payment_stores.payment_cancel_requests.payment_cancel_requests.values())
    )
    assert cancel_request.requested_by == "admin"
    assert cancel_request.operator_audit_id == result.operator_audit_id
    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        result.operator_audit_id
    ]
    assert audit.operator_id == "admin_1"
    assert audit.action == "payment.cancel"
    assert audit.target_type == "payment"
    assert audit.target_id == order.payment_id
    assert audit.previous_state["status"] == "paid"
    assert audit.next_state["cancel_reason"] == "duplicate_payment"
    assert audit.next_state["notification"] == {
        "template": "payment_cancel_completed",
        "queued": True,
        "payload": {"cancelAmount": order.amount},
    }
    assert audit.reason_code == "duplicate_payment"
    assert audit.idempotency_scope == "admin-payment-cancel"
    assert audit.request_ip == "203.0.113.10"


async def test_cancel_admin_payment_replays_same_idempotency_key(
    test_dependencies,
) -> None:
    order = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=[PaymentOrderItem(sku_id="sku_report_pack_100", quantity=2)],
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=order.payment_id,
            payment_key="paykey_admin",
            order_id=order.order_id,
            amount=order.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-admin-key",
    )
    kwargs = {
        "context": AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        "payment_id": order.payment_id,
        "command": AdminPaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="duplicate_payment",
            reason_message="duplicate order",
            notify_customer=True,
        ),
        "one_time_payment_uow_factory": test_dependencies.one_time_payment_uow_factory,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "idempotency_key": "admin-cancel-key",
    }

    first = await cancel_admin_payment(**kwargs)
    second = await cancel_admin_payment(**kwargs)

    assert second == first
    assert test_dependencies.payment_provider.cancel_payment_call_count == 1


async def test_cancel_admin_payment_records_unqueued_notification_when_disabled(
    test_dependencies,
) -> None:
    order = await create_confirmed_admin_cancel_payment(
        test_dependencies,
        request_id="req_1",
        confirm_request_id="req_2",
        payment_key="paykey_admin",
        confirm_key="confirm-admin-key",
    )

    result = await cancel_admin_payment(
        AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        order.payment_id,
        AdminPaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="operator_adjustment",
            reason_message="manual no-notify correction",
            notify_customer=False,
        ),
        test_dependencies.one_time_payment_uow_factory,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        idempotency_key="admin-cancel-no-notify",
    )

    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        result.operator_audit_id
    ]
    assert audit.next_state["notify_customer"] is False
    assert audit.next_state["notification"] == {
        "template": "payment_cancel_completed",
        "queued": False,
        "payload": {"cancelAmount": order.amount},
    }


async def test_cancel_admin_payment_scopes_idempotency_key_by_payment_id(
    test_dependencies,
) -> None:
    first = await create_confirmed_admin_cancel_payment(
        test_dependencies,
        request_id="req_1",
        confirm_request_id="req_2",
        payment_key="paykey_admin_1",
        confirm_key="confirm-admin-key-1",
    )
    second = await create_confirmed_admin_cancel_payment(
        test_dependencies,
        request_id="req_3",
        confirm_request_id="req_4",
        payment_key="paykey_admin_2",
        confirm_key="confirm-admin-key-2",
    )

    first_cancel = await cancel_admin_payment(
        AdminRequestContext(request_id="req_admin_1", admin_id="admin_1"),
        first.payment_id,
        AdminPaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="duplicate_payment",
            reason_message="duplicate order",
            notify_customer=True,
        ),
        test_dependencies.one_time_payment_uow_factory,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        idempotency_key="shared-admin-cancel-key",
    )
    second_cancel = await cancel_admin_payment(
        AdminRequestContext(request_id="req_admin_2", admin_id="admin_1"),
        second.payment_id,
        AdminPaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="duplicate_payment",
            reason_message="duplicate order",
            notify_customer=True,
        ),
        test_dependencies.one_time_payment_uow_factory,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        idempotency_key="shared-admin-cancel-key",
    )

    assert first_cancel.payment_id == first.payment_id
    assert second_cancel.payment_id == second.payment_id
    assert first_cancel.status == "canceled"
    assert second_cancel.status == "canceled"
    assert test_dependencies.payment_provider.cancel_payment_call_count == 2


async def test_cancel_admin_payment_rejects_when_payment_cancel_is_locked(
    test_dependencies,
) -> None:
    order = await create_confirmed_admin_cancel_payment(
        test_dependencies,
        request_id="req_1",
        confirm_request_id="req_2",
        payment_key="paykey_admin",
        confirm_key="confirm-admin-key",
    )
    lock_key = f"payment-cancel:{order.payment_id}"
    operation_lock = await test_dependencies.operation_locks.acquire_operation_lock(
        lock_key=lock_key,
        owner_token="other-owner",
        fencing_counter_key="payment-cancel",
        locked_until_at=test_dependencies.clock.utc_now(),
        acquired_at=test_dependencies.clock.utc_now(),
    )
    assert operation_lock is not None
    operation_lock.locked_until_at = operation_lock.locked_until_at.replace(
        minute=operation_lock.locked_until_at.minute + 5
    )

    with pytest.raises(InvalidStateTransitionError):
        await cancel_admin_payment(
            AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
            order.payment_id,
            AdminPaymentCancelCommand(
                cancel_amount=None,
                cancel_reason="duplicate_payment",
                reason_message="duplicate order",
                notify_customer=True,
            ),
            test_dependencies.one_time_payment_uow_factory,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            idempotency_key="admin-cancel-key",
            operation_locks=test_dependencies.operation_locks,
        )

    assert test_dependencies.payment_provider.cancel_payment_call_count == 0
    assert test_dependencies.operation_locks.release_calls == []


async def test_cancel_admin_payment_rejects_invalid_cancel_amount(
    test_dependencies,
) -> None:
    order = await create_confirmed_admin_cancel_payment(
        test_dependencies,
        request_id="req_1",
        confirm_request_id="req_2",
        payment_key="paykey_admin",
        confirm_key="confirm-admin-key",
    )

    for cancel_amount in (0, order.amount + 1):
        with pytest.raises(BadRequestError):
            await cancel_admin_payment(
                AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
                order.payment_id,
                AdminPaymentCancelCommand(
                    cancel_amount=cancel_amount,
                    cancel_reason="duplicate_payment",
                    reason_message="duplicate order",
                    notify_customer=True,
                ),
                test_dependencies.one_time_payment_uow_factory,
                test_dependencies.payment_provider,
                test_dependencies.clock,
                idempotency_key=f"admin-cancel-key-{cancel_amount}",
                operation_locks=test_dependencies.operation_locks,
            )

    assert test_dependencies.payment_provider.cancel_payment_call_count == 0


async def test_cancel_admin_payment_rejects_idempotency_conflict(
    test_dependencies,
) -> None:
    order = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=[PaymentOrderItem(sku_id="sku_report_pack_100", quantity=2)],
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=order.payment_id,
            payment_key="paykey_admin",
            order_id=order.order_id,
            amount=order.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-admin-key",
    )
    kwargs = {
        "context": AdminRequestContext(request_id="req_admin", admin_id="admin_1"),
        "payment_id": order.payment_id,
        "command": AdminPaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="duplicate_payment",
            reason_message="duplicate order",
            notify_customer=True,
        ),
        "one_time_payment_uow_factory": test_dependencies.one_time_payment_uow_factory,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "idempotency_key": "admin-cancel-key",
    }
    await cancel_admin_payment(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        await cancel_admin_payment(
            **{
                **kwargs,
                "command": AdminPaymentCancelCommand(
                    cancel_amount=1,
                    cancel_reason="duplicate_payment",
                    reason_message="duplicate order",
                    notify_customer=True,
                ),
            }
        )


async def test_cancel_admin_payment_records_failed_audit_on_provider_error(
    test_dependencies,
) -> None:
    order = await create_confirmed_admin_cancel_payment(
        test_dependencies,
        request_id="req_1",
        confirm_request_id="req_2",
        payment_key="paykey_admin",
        confirm_key="confirm-admin-key",
    )
    test_dependencies.payment_provider.cancel_payment_error = ProviderError(
        "provider unavailable",
        provider_code="PROVIDER_TIMEOUT",
        retryable=True,
    )

    with pytest.raises(ProviderError):
        await cancel_admin_payment(
            AdminRequestContext(
                request_id="req_admin",
                admin_id="admin_1",
                request_ip="203.0.113.11",
            ),
            order.payment_id,
            AdminPaymentCancelCommand(
                cancel_amount=None,
                cancel_reason="duplicate_payment",
                reason_message="duplicate order",
                notify_customer=True,
            ),
            test_dependencies.one_time_payment_uow_factory,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            idempotency_key="admin-cancel-key",
        )

    cancel_request = next(
        iter(test_dependencies.payment_stores.payment_cancel_requests.payment_cancel_requests.values())
    )
    assert cancel_request.status == "failed"
    assert cancel_request.failure == {
        "message": "provider cancel failed",
        "providerCode": "PROVIDER_TIMEOUT",
        "retryable": True,
    }
    assert cancel_request.operator_audit_id is not None
    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        cancel_request.operator_audit_id
    ]
    assert audit.operator_id == "admin_1"
    assert audit.action == "payment.cancel"
    assert audit.target_id == order.payment_id
    assert audit.result == "failed"
    assert audit.reason_code == "duplicate_payment"
    assert audit.reason_message == "duplicate order"
    assert audit.request_ip == "203.0.113.11"
    assert audit.next_state["failure"]["message"] == "provider cancel failed"
    assert audit.next_state["failure"]["providerCode"] == "PROVIDER_TIMEOUT"


async def test_cancel_admin_payment_rejects_duplicate_provider_cancel_id(
    test_dependencies,
) -> None:
    order = await create_confirmed_admin_cancel_payment(
        test_dependencies,
        request_id="req_1",
        confirm_request_id="req_2",
        payment_key="paykey_admin",
        confirm_key="confirm-admin-key",
    )
    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_duplicate_admin",
            cancel_amount=10_000,
            canceled_amount=10_000,
            cancelable_amount=40_000,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/cancel-1",
        )
    )
    await cancel_admin_payment(
        AdminRequestContext(request_id="req_admin_1", admin_id="admin_1"),
        order.payment_id,
        AdminPaymentCancelCommand(
            cancel_amount=10_000,
            cancel_reason="duplicate_payment",
            reason_message="first partial cancel",
            notify_customer=True,
        ),
        test_dependencies.one_time_payment_uow_factory,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        idempotency_key="admin-cancel-key-1",
    )

    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_duplicate_admin",
            cancel_amount=5_000,
            canceled_amount=15_000,
            cancelable_amount=35_000,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/cancel-2",
        )
    )
    with pytest.raises(ProviderError):
        await cancel_admin_payment(
            AdminRequestContext(
                request_id="req_admin_2",
                admin_id="admin_1",
                request_ip="203.0.113.12",
            ),
            order.payment_id,
            AdminPaymentCancelCommand(
                cancel_amount=5_000,
                cancel_reason="duplicate_payment",
                reason_message="duplicate provider cancel id",
                notify_customer=True,
            ),
            test_dependencies.one_time_payment_uow_factory,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            idempotency_key="admin-cancel-key-2",
        )

    payment = test_dependencies.payment_stores.payments.payments[order.payment_id]
    assert len(payment.cancel_history or []) == 1
    cancel_requests = (
        test_dependencies.payment_stores.payment_cancel_requests.payment_cancel_requests
    )
    failed_request = next(
        request
        for request in cancel_requests.values()
        if request.cancel_amount == 5_000
    )
    assert failed_request.status == "failed"
    assert failed_request.failure == {
        "message": "provider cancel id is duplicated",
        "retryable": True,
    }
    assert failed_request.operator_audit_id is not None
    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        failed_request.operator_audit_id
    ]
    assert audit.result == "failed"
    assert audit.request_ip == "203.0.113.12"
    assert audit.next_state["failure"]["message"] == (
        "provider cancel id is duplicated"
    )


async def test_adjust_admin_subscription_postpones_next_billing(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_1",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    result = await adjust_admin_subscription(
        AdminRequestContext(
            request_id="req_admin_adjust",
            admin_id="admin_1",
            request_ip="203.0.113.20",
        ),
        subscription.id,
        AdminSubscriptionAdjustCommand(
            adjustment_type="postpone_next_billing",
            reason_code="service_incident_compensation",
            reason_message="2026-06-08 incident compensation",
            postpone_days=7,
            notify_customer=True,
        ),
        test_dependencies.admin_operations,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        idempotency_key="adjust-key-1",
        operation_locks=test_dependencies.operation_locks,
    )

    saved = test_dependencies.admin_operations.subscriptions[subscription.id]
    assert saved.next_billing_at == datetime(2026, 7, 8, tzinfo=UTC)
    assert result.subscription_id == subscription.id
    assert result.previous_state == {
        "status": "active",
        "nextBillingAt": datetime(2026, 7, 1, tzinfo=UTC),
    }
    assert result.current_state == {
        "status": "active",
        "nextBillingAt": datetime(2026, 7, 8, tzinfo=UTC),
    }
    assert result.operator_audit_id.startswith("audit_")
    assert result.notified_customer is True
    assert test_dependencies.admin_operations.audit_records[0]["request_ip"] == (
        "203.0.113.20"
    )
    assert test_dependencies.admin_operations.audit_records[0]["admin_id"] == "admin_1"
    idempotency_key_id = test_dependencies.admin_operations.audit_records[0][
        "idempotency_key_id"
    ]
    assert isinstance(idempotency_key_id, str)
    assert idempotency_key_id.startswith("idem_")
    assert test_dependencies.admin_operations.audit_records[0][
        "idempotency_scope"
    ] == "admin-subscription-adjust"
    assert test_dependencies.admin_operations.audit_records[0][
        "idempotency_key_hash"
    ]
    assert test_dependencies.admin_operations.audit_records[0][
        "idempotency_request_hash"
    ]
    assert test_dependencies.admin_operations.audit_records[0]["next"][
        "notification"
    ] == {
        "template": "subscription_adjustment_completed",
        "queued": True,
        "payload": {
            "adjustmentType": "postpone_next_billing",
            "status": "active",
            "nextBillingAt": datetime(2026, 7, 8, tzinfo=UTC),
        },
    }
    assert test_dependencies.operation_locks.acquire_calls == [
        "subscription:sub_adjust_1"
    ]
    assert test_dependencies.operation_locks.release_calls == [
        "subscription:sub_adjust_1"
    ]


async def test_adjust_admin_subscription_reads_current_state_after_lock(
    test_dependencies,
) -> None:
    stale_subscription = Subscription(
        id="sub_adjust_lock_fresh",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    current_subscription = Subscription(
        id=stale_subscription.id,
        user_id=stale_subscription.user_id,
        payment_customer_id=stale_subscription.payment_customer_id,
        plan_id=stale_subscription.plan_id,
        product_code=stale_subscription.product_code,
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
        current_period_start_at=stale_subscription.current_period_start_at,
        current_period_end_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[stale_subscription.id] = (
        stale_subscription
    )
    operation_locks = MutatingOperationLockRepository(
        repository=test_dependencies.admin_operations,
        replacement_subscription=current_subscription,
    )

    result = await adjust_admin_subscription(
        AdminRequestContext(
            request_id="req_admin_adjust_after_lock",
            admin_id="admin_1",
        ),
        stale_subscription.id,
        AdminSubscriptionAdjustCommand(
            adjustment_type="postpone_next_billing",
            reason_code="service_incident_compensation",
            reason_message="postpone after concurrent admin adjustment",
            postpone_days=7,
        ),
        test_dependencies.admin_operations,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        idempotency_key="adjust-key-after-lock",
        operation_locks=operation_locks,
    )

    saved = test_dependencies.admin_operations.subscriptions[stale_subscription.id]
    assert saved.next_billing_at == datetime(2026, 7, 17, tzinfo=UTC)
    assert result.previous_state == {
        "status": "active",
        "nextBillingAt": datetime(2026, 7, 10, tzinfo=UTC),
    }
    assert result.current_state == {
        "status": "active",
        "nextBillingAt": datetime(2026, 7, 17, tzinfo=UTC),
    }
    assert operation_locks.acquire_calls == ["subscription:sub_adjust_lock_fresh"]
    assert operation_locks.release_calls == ["subscription:sub_adjust_lock_fresh"]


async def test_adjust_admin_subscription_rejects_postpone_for_cancel_scheduled(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_postpone_cancel_scheduled",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="cancel_scheduled",
        cancel_at_period_end=True,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    with pytest.raises(InvalidStateTransitionError):
        await adjust_admin_subscription(
            AdminRequestContext(request_id="req_admin_postpone", admin_id="admin_1"),
            subscription.id,
            AdminSubscriptionAdjustCommand(
                adjustment_type="postpone_next_billing",
                reason_code="service_incident_compensation",
                reason_message="2026-06-08 incident compensation",
                postpone_days=7,
            ),
            test_dependencies.admin_operations,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.clock,
            idempotency_key="adjust-postpone-cancel-scheduled",
        )

    assert (
        test_dependencies.admin_operations.subscriptions[
            subscription.id
        ].next_billing_at
        == datetime(2026, 7, 1, tzinfo=UTC)
    )


async def test_adjust_admin_subscription_rejects_invalid_request_values(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_adjust_invalid_request",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    invalid_commands = [
        AdminSubscriptionAdjustCommand(
            adjustment_type="postpone_next_billing",
            reason_code="service_incident_compensation",
            reason_message="missing postpone days",
        ),
        AdminSubscriptionAdjustCommand(
            adjustment_type="postpone_next_billing",
            reason_code="service_incident_compensation",
            reason_message="zero postpone days",
            postpone_days=0,
        ),
        AdminSubscriptionAdjustCommand(
            adjustment_type="postpone_next_billing",
            reason_code="service_incident_compensation",
            reason_message="too many postpone days",
            postpone_days=91,
        ),
        AdminSubscriptionAdjustCommand(
            adjustment_type="provider_payment_sync",
            reason_code="webhook_recovery",
            reason_message="missing payment evidence",
        ),
        AdminSubscriptionAdjustCommand(
            adjustment_type="set_next_billing_date",
            reason_code="migration_fix",
            reason_message="missing absolute date",
        ),
        AdminSubscriptionAdjustCommand(
            adjustment_type="set_next_billing_date",
            reason_code="migration_fix",
            reason_message="past absolute date",
            next_billing_at=datetime(2026, 6, 9, tzinfo=UTC),
        ),
        AdminSubscriptionAdjustCommand(
            adjustment_type="set_next_billing_date",
            reason_code="migration_fix",
            reason_message="too far absolute date",
            next_billing_at=datetime(2027, 7, 1, tzinfo=UTC),
        ),
        AdminSubscriptionAdjustCommand(
            adjustment_type="status_override",
            reason_code="cs_exception",
            reason_message="missing target status",
        ),
    ]

    for index, command in enumerate(invalid_commands):
        with pytest.raises(BadRequestError):
            await adjust_admin_subscription(
                AdminRequestContext(
                    request_id=f"req_admin_invalid_{index}",
                    admin_id="admin_1",
                ),
                subscription.id,
                command,
                test_dependencies.admin_operations,
                test_dependencies.payment_stores.idempotency_keys,
                test_dependencies.clock,
                idempotency_key=f"adjust-invalid-request-{index}",
                operation_locks=test_dependencies.operation_locks,
                provider=test_dependencies.payment_provider,
            )

    assert test_dependencies.admin_operations.audit_records == []


async def test_adjust_admin_subscription_syncs_provider_done_payment(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_provider_sync",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_provider_sync",
        order_id="order_sync",
        amount=9900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    invoice = Invoice(
        id="inv_provider_sync",
        user_id=subscription.user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    test_dependencies.admin_operations.payments[payment.id] = payment
    test_dependencies.admin_operations.invoices[invoice.id] = invoice
    test_dependencies.payment_provider.get_payment_result = PaymentLookupProviderResult(
        payment_key="paykey_provider_done",
        order_id=payment.order_id,
        status="DONE",
        total_amount=payment.amount,
        approved_at=datetime(2026, 6, 10, 1, 30, tzinfo=UTC),
        receipt_url="https://dashboard.tosspayments.com/receipt/provider-sync",
        method="카드",
        method_detail={"maskedCardNumber": "**** **** **** 4242"},
        response_summary={
            "provider": "tosspayments",
            "status": "DONE",
            "paymentKey": "paykey_provider_done",
        },
        cancelable_amount=payment.amount,
    )
    observed_processing_key = False

    def assert_processing_key_saved_before_provider_lookup() -> None:
        nonlocal observed_processing_key
        stored_keys = (
            test_dependencies
            .payment_stores
            .idempotency_keys
            .idempotency_keys
            .values()
        )
        keys = [
            key
            for key in stored_keys
            if key.scope == ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE
        ]
        assert len(keys) == 1
        assert keys[0].status == "processing"
        assert keys[0].resource_type == "subscription_adjustment"
        assert keys[0].resource_id is not None
        observed_processing_key = True

    test_dependencies.payment_provider.before_get_payment = (
        assert_processing_key_saved_before_provider_lookup
    )

    result = await adjust_admin_subscription(
        AdminRequestContext(request_id="req_provider_sync", admin_id="admin_1"),
        subscription.id,
        AdminSubscriptionAdjustCommand(
            adjustment_type="provider_payment_sync",
            payment_key="paykey_provider_done",
            invoice_id=invoice.id,
            reason_code="webhook_recovery",
            reason_message="provider DONE was not reflected internally",
            notify_customer=True,
        ),
        test_dependencies.admin_operations,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        idempotency_key="adjust-provider-sync",
        provider=test_dependencies.payment_provider,
    )

    saved_payment = test_dependencies.admin_operations.payments[payment.id]
    saved_invoice = test_dependencies.admin_operations.invoices[invoice.id]
    saved_subscription = test_dependencies.admin_operations.subscriptions[
        subscription.id
    ]
    assert test_dependencies.payment_provider.get_payment_call_count == 1
    assert observed_processing_key is True
    stored_keys = (
        test_dependencies.payment_stores.idempotency_keys.idempotency_keys.values()
    )
    idempotency_key = next(
        key
        for key in stored_keys
        if key.scope == ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE
    )
    assert idempotency_key.status == "succeeded"
    assert idempotency_key.response_status == 200
    assert saved_payment.status == "paid"
    assert saved_payment.payment_key == "paykey_provider_done"
    assert saved_payment.approved_at == datetime(2026, 6, 10, 1, 30, tzinfo=UTC)
    assert saved_payment.cancelable_amount == 9900
    assert saved_invoice.status == "paid"
    assert saved_invoice.receipt_url == (
        "https://dashboard.tosspayments.com/receipt/provider-sync"
    )
    assert saved_subscription.status == "active"
    assert saved_subscription.current_period_start_at == datetime(
        2026, 6, 10, 1, 30, tzinfo=UTC
    )
    assert saved_subscription.next_billing_at == datetime(
        2026, 7, 10, 1, 30, tzinfo=UTC
    )
    assert result.current_state["paymentStatus"] == "paid"
    assert result.current_state["invoiceStatus"] == "paid"
    assert result.current_state["providerPaymentKey"] == "paykey_provider_done"
    assert test_dependencies.admin_operations.audit_records[0]["result"] == "succeeded"


async def test_adjust_admin_subscription_provider_sync_records_failed_audit_on_mismatch(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_provider_mismatch",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_provider_mismatch",
        order_id="order_sync_mismatch",
        amount=9900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    invoice = Invoice(
        id="inv_provider_mismatch",
        user_id=subscription.user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    test_dependencies.admin_operations.payments[payment.id] = payment
    test_dependencies.admin_operations.invoices[invoice.id] = invoice
    test_dependencies.payment_provider.get_payment_result = PaymentLookupProviderResult(
        payment_key="paykey_provider_mismatch",
        order_id=payment.order_id,
        status="DONE",
        total_amount=payment.amount + 1000,
        approved_at=datetime(2026, 6, 10, 1, 30, tzinfo=UTC),
        receipt_url="https://dashboard.tosspayments.com/receipt/provider-sync",
        method="카드",
        method_detail={},
        response_summary={"provider": "tosspayments", "status": "DONE"},
    )

    with pytest.raises(ProviderError):
        await adjust_admin_subscription(
            AdminRequestContext(request_id="req_provider_mismatch", admin_id="admin_1"),
            subscription.id,
            AdminSubscriptionAdjustCommand(
                adjustment_type="provider_payment_sync",
                payment_key="paykey_provider_mismatch",
                invoice_id=invoice.id,
                reason_code="payment_sync_mismatch",
                reason_message="provider amount mismatch",
            ),
            test_dependencies.admin_operations,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.clock,
            idempotency_key="adjust-provider-mismatch",
            provider=test_dependencies.payment_provider,
        )

    assert test_dependencies.admin_operations.payments[payment.id].status == "failed"
    assert test_dependencies.admin_operations.invoices[invoice.id].status == "issued"
    assert (
        test_dependencies.admin_operations.subscriptions[subscription.id].status
        == "past_due"
    )
    assert test_dependencies.admin_operations.audit_records[0]["result"] == "failed"
    assert test_dependencies.admin_operations.audit_records[0]["next"]["failure"] == {
        "message": "provider payment amount mismatch"
    }
    stored_keys = (
        test_dependencies.payment_stores.idempotency_keys.idempotency_keys.values()
    )
    idempotency_key = next(
        key
        for key in stored_keys
        if key.scope == ADMIN_SUBSCRIPTION_ADJUST_IDEMPOTENCY_SCOPE
    )
    assert idempotency_key.status == "failed"
    assert idempotency_key.response_body is None


async def test_adjust_admin_subscription_clears_payment_failure(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_past_due",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription
    payment = Payment(
        id="pay_clear_failure",
        order_id="order_clear_failure",
        amount=9900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 11, tzinfo=UTC),
        failure={"code": "CARD_DECLINED", "message": "card declined"},
    )
    invoice = Invoice(
        id="inv_clear_failure",
        user_id=subscription.user_id,
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    test_dependencies.admin_operations.payments[payment.id] = payment
    test_dependencies.admin_operations.invoices[invoice.id] = invoice

    result = await adjust_admin_subscription(
        AdminRequestContext(request_id="req_admin_clear", admin_id="admin_1"),
        subscription.id,
        AdminSubscriptionAdjustCommand(
            adjustment_type="clear_payment_failure",
            invoice_id=invoice.id,
            reason_code="retry_recovered",
            reason_message="Customer paid via manual recovery",
            target_status="active",
        ),
        test_dependencies.admin_operations,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        idempotency_key="adjust-key-2",
    )

    assert (
        test_dependencies.admin_operations.subscriptions[subscription.id].status
        == "active"
    )
    saved_payment = test_dependencies.admin_operations.payments[payment.id]
    saved_invoice = test_dependencies.admin_operations.invoices[invoice.id]
    repository = test_dependencies.admin_operations
    latest_failed_payment, latest_failed_invoice = (
        await repository.get_admin_latest_failed_subscription_payment(subscription.id)
    )
    assert latest_failed_payment is None
    assert latest_failed_invoice is None
    assert saved_payment.status == "paid"
    assert saved_payment.retry_scheduled_at is None
    assert saved_payment.failure is None
    assert saved_invoice.status == "paid"
    assert result.previous_state["status"] == "past_due"
    assert result.previous_state["paymentStatus"] == "failed"
    assert result.previous_state["invoiceStatus"] == "issued"
    assert result.previous_state["retryAt"] == datetime(2026, 6, 11, tzinfo=UTC)
    assert result.previous_state["paymentFailureReason"] == {
        "code": "CARD_DECLINED",
        "message": "card declined",
    }
    assert result.current_state["status"] == "active"
    assert result.current_state["paymentStatus"] == "paid"
    assert result.current_state["invoiceStatus"] == "paid"
    assert result.current_state["retryAt"] is None
    assert result.current_state["paymentFailureReason"] is None


async def test_adjust_admin_subscription_sets_absolute_next_billing_date(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_set_next",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    result = await adjust_admin_subscription(
        AdminRequestContext(request_id="req_admin_set", admin_id="admin_1"),
        subscription.id,
        AdminSubscriptionAdjustCommand(
            adjustment_type="set_next_billing_date",
            reason_code="migration_fix",
            reason_message="Restore migrated next billing date",
            next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        test_dependencies.admin_operations,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        idempotency_key="adjust-key-3",
    )

    assert result.current_state["nextBillingAt"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert "notification" not in result.current_state
    assert test_dependencies.admin_operations.audit_records[0]["next"][
        "notification"
    ] == {
        "template": "subscription_adjustment_completed",
        "queued": False,
        "payload": {
            "adjustmentType": "set_next_billing_date",
            "status": "active",
            "nextBillingAt": datetime(2026, 8, 1, tzinfo=UTC),
        },
    }


async def test_adjust_admin_subscription_rejects_set_next_for_cancel_scheduled(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_set_next_cancel_scheduled",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="cancel_scheduled",
        cancel_at_period_end=True,
        next_billing_at=None,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    with pytest.raises(InvalidStateTransitionError):
        await adjust_admin_subscription(
            AdminRequestContext(
                request_id="req_admin_set_cancel_scheduled",
                admin_id="admin_1",
            ),
            subscription.id,
            AdminSubscriptionAdjustCommand(
                adjustment_type="set_next_billing_date",
                reason_code="migration_fix",
                reason_message=(
                    "Do not restore billing date on cancel scheduled subscription"
                ),
                next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            test_dependencies.admin_operations,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.clock,
            idempotency_key="adjust-set-next-cancel-scheduled",
        )

    saved = test_dependencies.admin_operations.subscriptions[subscription.id]
    assert saved.status == "cancel_scheduled"
    assert saved.next_billing_at is None


async def test_adjust_admin_subscription_overrides_status(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_status_override",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    result = await adjust_admin_subscription(
        AdminRequestContext(request_id="req_admin_status", admin_id="admin_1"),
        subscription.id,
        AdminSubscriptionAdjustCommand(
            adjustment_type="status_override",
            reason_code="cs_exception",
            reason_message="Terminate subscription after refund exception",
            target_status="canceled",
        ),
        test_dependencies.admin_operations,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        idempotency_key="adjust-key-4",
    )

    saved = test_dependencies.admin_operations.subscriptions[subscription.id]
    assert saved.status == "canceled"
    assert saved.next_billing_at is None
    assert result.current_state == {"status": "canceled", "nextBillingAt": None}


async def test_adjust_admin_subscription_rejects_invalid_status_override_transition(
    test_dependencies,
) -> None:
    subscription = Subscription(
        id="sub_status_invalid_override",
        user_id="user_1",
        payment_customer_id="customer_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="canceled",
        cancel_at_period_end=False,
        next_billing_at=None,
    )
    test_dependencies.admin_operations.subscriptions[subscription.id] = subscription

    with pytest.raises(InvalidStateTransitionError):
        await adjust_admin_subscription(
            AdminRequestContext(
                request_id="req_admin_status_invalid",
                admin_id="admin_1",
            ),
            subscription.id,
            AdminSubscriptionAdjustCommand(
                adjustment_type="status_override",
                reason_code="cs_exception",
                reason_message="Reopen canceled subscription",
                target_status="active",
            ),
            test_dependencies.admin_operations,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.clock,
            idempotency_key="adjust-invalid-status",
        )

    assert (
        test_dependencies.admin_operations.subscriptions[subscription.id].status
        == "canceled"
    )
