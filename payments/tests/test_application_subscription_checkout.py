from __future__ import annotations

from datetime import UTC, datetime

import pytest

from payments.application.context import RequestContext
from payments.application.errors import (
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentRequiredResponseError,
    ProviderError,
)
from payments.application.ports import BillingChargeProviderResult
from payments.application.subscription_checkout import (
    SubscriptionCheckoutCommand,
    SubscriptionConfirmCommand,
    confirm_subscription_checkout,
    create_subscription_checkout,
)
from payments.domain.entities.subscription import Subscription


class FakeSubscriptionCheckoutRepository:
    def __init__(self) -> None:
        self.active_counts: dict[tuple[str, str], int] = {}
        self.subscriptions: dict[str, Subscription] = {}

    async def count_active_subscriptions_for_user_product(
        self,
        user_id: str,
        product_code: str,
    ) -> int:
        return self.active_counts.get(
            (user_id, product_code),
            sum(
                1
                for subscription in self.subscriptions.values()
                if subscription.user_id == user_id
                and subscription.product_code == product_code
                and subscription.status
                in {"pending", "active", "past_due", "cancel_scheduled"}
            ),
        )

    async def save_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription

    async def get_subscription_for_user(
        self,
        subscription_id: str,
        user_id: str,
    ) -> Subscription | None:
        subscription = self.subscriptions.get(subscription_id)
        if subscription is None or subscription.user_id != user_id:
            return None
        return subscription

    async def get_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def save_payment(self, payment) -> None:
        return None

    async def save_invoice(self, invoice) -> None:
        return None

    async def get_open_invoice_for_subscription_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ):
        return None


async def test_create_subscription_checkout_saves_pending_subscription(
    test_dependencies,
) -> None:
    result = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    subscription = test_dependencies.subscription_checkouts.subscriptions[
        result.subscription_id
    ]
    customer = next(
        iter(
            test_dependencies.payment_stores.payment_customers.payment_customers.values()
        )
    )

    assert result.subscription_id.startswith("sub_")
    assert result.customer_key == customer.customer_key
    assert result.product_code == "basic"
    assert result.amount == 9900
    assert result.currency == "KRW"
    assert result.client_key == "test_ck_local"
    assert result.success_url.endswith(f"subscriptionId={result.subscription_id}")
    assert subscription.payment_customer_id == customer.id


async def test_create_subscription_checkout_replays_same_idempotency_key(
    test_dependencies,
) -> None:
    kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "command": SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        "catalog": test_dependencies.catalog_repository,
        "subscriptions": test_dependencies.subscription_checkouts,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "clock": test_dependencies.clock,
        "client_key": "test_ck_local",
        "idempotency_key": "subscription-checkout-key",
    }

    first = await create_subscription_checkout(**kwargs)
    second = await create_subscription_checkout(**kwargs)

    assert second == first
    assert len(test_dependencies.subscription_checkouts.subscriptions) == 1


async def test_create_subscription_checkout_rejects_idempotency_conflict(
    test_dependencies,
) -> None:
    kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "command": SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        "catalog": test_dependencies.catalog_repository,
        "subscriptions": test_dependencies.subscription_checkouts,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "clock": test_dependencies.clock,
        "client_key": "test_ck_local",
        "idempotency_key": "subscription-checkout-key",
    }
    await create_subscription_checkout(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        await create_subscription_checkout(
            **{
                **kwargs,
                "command": SubscriptionCheckoutCommand(
                    plan_id="plan_basic_monthly",
                    success_url="https://example.com/other/success",
                    fail_url="https://example.com/subscription/fail",
                ),
            }
        )


async def test_create_subscription_checkout_rejects_unavailable_plan(
    test_dependencies,
) -> None:
    test_dependencies.catalog_repository.plans["plan_basic_monthly"].status = "paused"

    with pytest.raises(InvalidStateTransitionError):
        await create_subscription_checkout(
            RequestContext(request_id="req_1", user_id="user_1"),
            SubscriptionCheckoutCommand(
                plan_id="plan_basic_monthly",
                success_url="https://example.com/subscription/success",
                fail_url="https://example.com/subscription/fail",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.clock,
            client_key="test_ck_local",
        )


async def test_create_subscription_checkout_rejects_existing_pending_subscription(
    test_dependencies,
) -> None:
    await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )

    with pytest.raises(InvalidStateTransitionError):
        await create_subscription_checkout(
            RequestContext(request_id="req_2", user_id="user_1"),
            SubscriptionCheckoutCommand(
                plan_id="plan_basic_monthly",
                success_url="https://example.com/subscription/success",
                fail_url="https://example.com/subscription/fail",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.clock,
            client_key="test_ck_local",
        )


async def test_confirm_subscription_checkout_activates_subscription(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )

    result = await confirm_subscription_checkout(
        RequestContext(request_id="req_2", user_id="user_1"),
        SubscriptionConfirmCommand(
            subscription_id=checkout.subscription_id,
            customer_key=checkout.customer_key,
            auth_key="auth_123",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.billing_auths,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        test_dependencies.subscription_confirm_uow_factory,
        idempotency_key="subscription-confirm-key",
        operation_locks=test_dependencies.operation_locks,
    )

    subscription = test_dependencies.subscription_checkouts.subscriptions[
        checkout.subscription_id
    ]
    payment = test_dependencies.subscription_checkouts.payments[result.payment_id]
    customer = next(
        iter(
            test_dependencies.payment_stores.payment_customers.payment_customers.values()
        )
    )
    assert result.subscription_id == checkout.subscription_id
    assert result.status == "active"
    assert result.payment_status == "paid"
    assert result.payment_id.startswith("pay_")
    assert result.invoice_id.startswith("inv_")
    assert result.next_billing_date == subscription.next_billing_at
    assert subscription.status == "active"
    assert subscription.current_period_start_at == test_dependencies.clock.utc_now()
    assert payment.payment_customer_id == customer.id
    assert payment.billing_method_id == test_dependencies.billing_auths.methods[0].id
    assert payment.billing_cycle_key is not None
    assert (
        test_dependencies.billing_auths.instruments[0].payment_customer_id
        == customer.id
    )
    assert test_dependencies.billing_auths.instruments[0].billing_key != (
        "billing_key_secret"
    )
    assert (
        test_dependencies.billing_key_cipher.decrypt(
            test_dependencies.billing_auths.instruments[0].billing_key
        )
        == "billing_key_secret"
    )
    assert test_dependencies.billing_auths.methods[0].payment_customer_id == customer.id
    assert test_dependencies.subscription_confirm_uow_factory.commit_count == 1
    assert (
        test_dependencies.payment_provider.last_billing_charge_idempotency_key
        == "subscription-confirm-key"
    )
    assert test_dependencies.operation_locks.acquire_calls == [
        f"subscriptions-confirm:{checkout.subscription_id}"
    ]
    assert test_dependencies.operation_locks.release_calls == [
        f"subscriptions-confirm:{checkout.subscription_id}"
    ]


async def test_confirm_subscription_checkout_rejects_when_subscription_is_locked(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    lock_key = f"subscriptions-confirm:{checkout.subscription_id}"
    operation_lock = await test_dependencies.operation_locks.acquire_operation_lock(
        lock_key=lock_key,
        owner_token="other-owner",
        fencing_counter_key="subscriptions-confirm",
        locked_until_at=test_dependencies.clock.utc_now(),
        acquired_at=test_dependencies.clock.utc_now(),
    )
    assert operation_lock is not None
    operation_lock.locked_until_at = operation_lock.locked_until_at.replace(
        minute=operation_lock.locked_until_at.minute + 5
    )

    with pytest.raises(InvalidStateTransitionError):
        await confirm_subscription_checkout(
            RequestContext(request_id="req_2", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
            operation_locks=test_dependencies.operation_locks,
        )

    assert test_dependencies.payment_provider.issue_billing_key_call_count == 0
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0
    assert test_dependencies.operation_locks.acquire_calls == [lock_key, lock_key]
    assert test_dependencies.operation_locks.release_calls == []


async def test_confirm_subscription_checkout_rejects_other_user_subscription(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )

    with pytest.raises(ForbiddenError):
        await confirm_subscription_checkout(
            RequestContext(request_id="req_2", user_id="user_2"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
        )


async def test_confirm_subscription_checkout_replays_same_idempotency_key(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    kwargs = {
        "requester": RequestContext(request_id="req_2", user_id="user_1"),
        "command": SubscriptionConfirmCommand(
            subscription_id=checkout.subscription_id,
            customer_key=checkout.customer_key,
            auth_key="auth_123",
        ),
        "catalog": test_dependencies.catalog_repository,
        "subscriptions": test_dependencies.subscription_checkouts,
        "billing_auths": test_dependencies.billing_auths,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "billing_key_cipher": test_dependencies.billing_key_cipher,
        "subscription_confirm_uow_factory": (
            test_dependencies.subscription_confirm_uow_factory
        ),
        "idempotency_key": "subscription-confirm-key",
    }

    first = await confirm_subscription_checkout(**kwargs)
    second = await confirm_subscription_checkout(**kwargs)

    assert second == first
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 1
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1
    assert len(test_dependencies.subscription_checkouts.payments) == 1
    assert len(test_dependencies.subscription_checkouts.invoices) == 1


async def test_confirm_subscription_checkout_replays_same_subscription_success(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    kwargs = {
        "requester": RequestContext(request_id="req_2", user_id="user_1"),
        "command": SubscriptionConfirmCommand(
            subscription_id=checkout.subscription_id,
            customer_key=checkout.customer_key,
            auth_key="auth_123",
        ),
        "catalog": test_dependencies.catalog_repository,
        "subscriptions": test_dependencies.subscription_checkouts,
        "billing_auths": test_dependencies.billing_auths,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "billing_key_cipher": test_dependencies.billing_key_cipher,
        "subscription_confirm_uow_factory": (
            test_dependencies.subscription_confirm_uow_factory
        ),
    }

    first = await confirm_subscription_checkout(
        **kwargs,
        idempotency_key="subscription-confirm-key-1",
    )
    second = await confirm_subscription_checkout(
        **{
            **kwargs,
            "requester": RequestContext(request_id="req_3", user_id="user_1"),
        },
        idempotency_key="subscription-confirm-key-2",
    )

    assert second == first
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 1
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1
    assert test_dependencies.subscription_confirm_uow_factory.commit_count == 1
    assert len(test_dependencies.subscription_checkouts.payments) == 1
    assert len(test_dependencies.subscription_checkouts.invoices) == 1


async def test_confirm_subscription_checkout_returns_402_on_billing_key_issue_failure(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    test_dependencies.payment_provider.issue_billing_key_error = ProviderError(
        "인증 시간이 만료되었습니다.",
        provider_code="INVALID_AUTH_KEY",
    )

    with pytest.raises(PaymentRequiredResponseError) as exc_info:
        await confirm_subscription_checkout(
            RequestContext(request_id="req_2", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
        )

    assert exc_info.value.response_body == {
        "subscriptionId": checkout.subscription_id,
        "status": "pending",
        "failure": {
            "code": "BILLING_KEY_ISSUE_FAILED",
            "providerCode": "INVALID_AUTH_KEY",
            "message": "인증 시간이 만료되었습니다.",
            "retryable": True,
        },
    }
    assert test_dependencies.subscription_checkouts.payments == {}
    assert test_dependencies.subscription_checkouts.invoices == {}

    with pytest.raises(PaymentRequiredResponseError):
        await confirm_subscription_checkout(
            RequestContext(request_id="req_3", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
        )
    assert test_dependencies.payment_provider.issue_billing_key_call_count == 1


async def test_confirm_subscription_checkout_records_first_payment_failure(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "잔액 부족",
        provider_code="INSUFFICIENT_FUNDS",
    )

    with pytest.raises(PaymentRequiredResponseError) as exc_info:
        await confirm_subscription_checkout(
            RequestContext(request_id="req_2", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
        )

    payment = next(iter(test_dependencies.subscription_checkouts.payments.values()))
    invoice = next(iter(test_dependencies.subscription_checkouts.invoices.values()))
    subscription = test_dependencies.subscription_checkouts.subscriptions[
        checkout.subscription_id
    ]
    assert subscription.status == "pending"
    assert payment.status == "failed"
    assert payment.failure == {
        "phase": "confirm",
        "reason": "provider_rejected",
        "providerCode": "INSUFFICIENT_FUNDS",
        "message": "잔액 부족",
        "retryable": True,
    }
    assert invoice.status == "issued"
    assert exc_info.value.response_body == {
        "subscriptionId": checkout.subscription_id,
        "status": "pending",
        "paymentStatus": "failed",
        "paymentId": payment.id,
        "invoiceId": invoice.id,
        "failure": {
            "code": "FIRST_PAYMENT_FAILED",
            "providerCode": "INSUFFICIENT_FUNDS",
            "message": "잔액 부족",
            "retryable": True,
        },
    }

    with pytest.raises(PaymentRequiredResponseError):
        await confirm_subscription_checkout(
            RequestContext(request_id="req_3", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
        )
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


async def test_confirm_subscription_checkout_records_mismatched_charge_failure(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    test_dependencies.payment_provider.charge_billing_key_result = (
        BillingChargeProviderResult(
            payment_key="paykey_billing_charge",
            order_id="ord_other",
            amount=9900,
            approved_at=datetime(2026, 6, 10, 0, 1, tzinfo=UTC),
            receipt_url="https://dashboard.tosspayments.com/receipt/billing",
            method="카드",
            method_detail={"maskedCardNumber": "**** **** **** 1234"},
            response_summary={"provider": "tosspayments"},
        )
    )

    with pytest.raises(PaymentRequiredResponseError) as exc_info:
        await confirm_subscription_checkout(
            RequestContext(request_id="req_2", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
        )

    body = exc_info.value.response_body
    assert body["subscriptionId"] == checkout.subscription_id
    assert body["status"] == "pending"
    assert body["paymentStatus"] == "failed"
    assert body["failure"] == {
        "code": "FIRST_PAYMENT_FAILED",
        "providerCode": "PROVIDER_BILLING_CHARGE_MISMATCH",
        "message": "provider billing charge response does not match request",
        "retryable": True,
    }
    payment = next(iter(test_dependencies.subscription_checkouts.payments.values()))
    invoice = next(iter(test_dependencies.subscription_checkouts.invoices.values()))
    assert payment.status == "failed"
    assert payment.failure == {
        "phase": "confirm",
        "reason": "provider_rejected",
        "providerCode": "PROVIDER_BILLING_CHARGE_MISMATCH",
        "message": "provider billing charge response does not match request",
        "retryable": True,
    }
    assert invoice.status == "issued"
    assert test_dependencies.billing_auths.instruments
    assert test_dependencies.billing_auths.methods

    with pytest.raises(PaymentRequiredResponseError):
        await confirm_subscription_checkout(
            RequestContext(request_id="req_3", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key",
        )
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


async def test_confirm_subscription_checkout_reuses_issued_invoice_on_retry_success(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "잔액 부족",
        provider_code="INSUFFICIENT_FUNDS",
    )

    with pytest.raises(PaymentRequiredResponseError) as failed_info:
        await confirm_subscription_checkout(
            RequestContext(request_id="req_2", user_id="user_1"),
            SubscriptionConfirmCommand(
                subscription_id=checkout.subscription_id,
                customer_key=checkout.customer_key,
                auth_key="auth_123",
            ),
            test_dependencies.catalog_repository,
            test_dependencies.subscription_checkouts,
            test_dependencies.billing_auths,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            test_dependencies.subscription_confirm_uow_factory,
            idempotency_key="subscription-confirm-key-1",
        )

    failed_invoice_id = str(failed_info.value.response_body["invoiceId"])
    failed_payment_id = str(failed_info.value.response_body["paymentId"])
    test_dependencies.payment_provider.charge_billing_key_error = None

    result = await confirm_subscription_checkout(
        RequestContext(request_id="req_3", user_id="user_1"),
        SubscriptionConfirmCommand(
            subscription_id=checkout.subscription_id,
            customer_key=checkout.customer_key,
            auth_key="auth_456",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.billing_auths,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        test_dependencies.subscription_confirm_uow_factory,
        idempotency_key="subscription-confirm-key-2",
    )

    invoice = test_dependencies.subscription_checkouts.invoices[failed_invoice_id]
    assert result.invoice_id == failed_invoice_id
    assert result.status == "active"
    assert invoice.status == "paid"
    assert invoice.payment_id == result.payment_id
    assert failed_payment_id in test_dependencies.subscription_checkouts.payments
    assert len(test_dependencies.subscription_checkouts.invoices) == 1
    assert len(test_dependencies.subscription_checkouts.payments) == 2


async def test_confirm_subscription_checkout_rejects_idempotency_conflict(
    test_dependencies,
) -> None:
    checkout = await create_subscription_checkout(
        RequestContext(request_id="req_1", user_id="user_1"),
        SubscriptionCheckoutCommand(
            plan_id="plan_basic_monthly",
            success_url="https://example.com/subscription/success",
            fail_url="https://example.com/subscription/fail",
        ),
        test_dependencies.catalog_repository,
        test_dependencies.subscription_checkouts,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.clock,
        client_key="test_ck_local",
    )
    kwargs = {
        "requester": RequestContext(request_id="req_2", user_id="user_1"),
        "command": SubscriptionConfirmCommand(
            subscription_id=checkout.subscription_id,
            customer_key=checkout.customer_key,
            auth_key="auth_123",
        ),
        "catalog": test_dependencies.catalog_repository,
        "subscriptions": test_dependencies.subscription_checkouts,
        "billing_auths": test_dependencies.billing_auths,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "billing_key_cipher": test_dependencies.billing_key_cipher,
        "subscription_confirm_uow_factory": (
            test_dependencies.subscription_confirm_uow_factory
        ),
        "idempotency_key": "subscription-confirm-key",
    }
    await confirm_subscription_checkout(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        await confirm_subscription_checkout(
            **{
                **kwargs,
                "command": SubscriptionConfirmCommand(
                    subscription_id=checkout.subscription_id,
                    customer_key=checkout.customer_key,
                    auth_key="auth_other",
                ),
            }
        )
