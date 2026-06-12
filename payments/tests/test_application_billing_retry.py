from __future__ import annotations

from datetime import UTC, datetime

import pytest

from payments.application.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ProviderError,
)
from payments.application.jobs.billing_retry import (
    BillingRetryCommand,
    retry_subscription_billing,
)
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.subscription import Subscription


async def test_retry_subscription_billing_marks_invoice_paid(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry",
        order_id="ord_retry",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name="Hyundai 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice
    repository.instruments[instrument.id] = instrument
    repository.billing_methods[method.id] = method

    result = await retry_subscription_billing(
        invoice.id,
        BillingRetryCommand(reason="scheduled_retry"),
        repository,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="billing-retry-key",
        operation_locks=test_dependencies.operation_locks,
    )

    assert result.invoice_id == invoice.id
    assert result.status == "active"
    assert result.invoice_status == "paid"
    assert result.payment_status == "paid"
    assert invoice.status == "paid"
    assert payment.status == "failed"
    assert invoice.payment_id != payment.id
    assert repository.payments[invoice.payment_id].status == "paid"
    assert repository.payments[invoice.payment_id].billing_method_id == method.id
    assert subscription.status == "active"
    assert result.receipt_url == "https://dashboard.tosspayments.com/receipt/billing"
    assert result.notification["template"] == "subscription_payment_paid"
    payload = result.notification["payload"]
    assert isinstance(payload, dict)
    assert payload["invoiceId"] == invoice.id
    assert payload["receiptUrl"] == result.receipt_url
    assert test_dependencies.payment_provider.last_billing_charge_customer_key == (
        payment_customer.customer_key
    )
    assert (
        test_dependencies.payment_provider.last_billing_charge_billing_key
        == "billing_key_secret"
    )
    assert (
        test_dependencies.payment_provider.last_billing_charge_idempotency_key
        == "billing-retry-key"
    )
    assert test_dependencies.operation_locks.acquire_calls == [
        "subscription-retry:inv_retry"
    ]
    assert test_dependencies.operation_locks.release_calls == [
        "subscription-retry:inv_retry"
    ]


async def test_retry_subscription_billing_replays_same_idempotency_key(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry",
        order_id="ord_retry",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name="Hyundai 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice
    repository.instruments[instrument.id] = instrument
    repository.billing_methods[method.id] = method
    kwargs = {
        "invoice_id": invoice.id,
        "command": BillingRetryCommand(reason="scheduled_retry"),
        "repository": repository,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "billing_key_cipher": test_dependencies.billing_key_cipher,
        "idempotency_key": "billing-retry-key",
    }

    first = await retry_subscription_billing(**kwargs)
    second = await retry_subscription_billing(**kwargs)

    assert second == first
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


async def test_retry_subscription_billing_records_failed_attempt_on_provider_error(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    previous_payment = Payment(
        id="pay_retry_previous",
        order_id="ord_retry_previous",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry",
        user_id="user_1",
        payment_id=previous_payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name="Hyundai 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[previous_payment.id] = previous_payment
    repository.invoices[invoice.id] = invoice
    repository.instruments[instrument.id] = instrument
    repository.billing_methods[method.id] = method
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "provider rejected billing retry"
    )

    result = await retry_subscription_billing(
        invoice.id,
        BillingRetryCommand(reason="scheduled_retry"),
        repository,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="billing-retry-key",
        operation_locks=test_dependencies.operation_locks,
    )

    latest_payment = repository.payments[invoice.payment_id]
    assert result.status == "past_due"
    assert result.invoice_status == "issued"
    assert result.payment_status == "failed"
    assert result.receipt_url is None
    assert invoice.status == "issued"
    assert invoice.payment_id != previous_payment.id
    assert latest_payment.status == "failed"
    assert latest_payment.amount == previous_payment.amount
    assert latest_payment.billing_method_id == method.id
    assert latest_payment.retry_scheduled_at == datetime(2026, 6, 11, tzinfo=UTC)
    assert latest_payment.failure == {
        "code": "BILLING_RETRY_FAILED",
        "providerCode": "PROVIDER_BILLING_RETRY_FAILED",
        "message": "provider rejected billing retry",
        "retryable": True,
        "phase": "billing_retry",
        "reason": "provider_error",
    }
    assert result.next_billing_date == latest_payment.retry_scheduled_at
    assert result.notification["template"] == "subscription_payment_failed"
    payload = result.notification["payload"]
    assert isinstance(payload, dict)
    assert payload["invoiceId"] == invoice.id
    assert payload["retryScheduledAt"] == "2026-06-11"
    assert payload["billingMethodUpdateUrl"] == "/billing/methods"
    assert test_dependencies.operation_locks.release_calls == [
        "subscription-retry:inv_retry"
    ]


async def test_retry_subscription_billing_cancels_subscription_after_final_failure(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    billing_cycle_key = "sub_retry:2026-06-10T00:00:00+00:00"
    first_failed_payment = Payment(
        id="pay_retry_first",
        order_id="ord_retry_first",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 8, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    latest_failed_payment = Payment(
        id="pay_retry_latest",
        order_id="ord_retry_latest",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry",
        user_id="user_1",
        payment_id=latest_failed_payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 8, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name="Hyundai 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[first_failed_payment.id] = first_failed_payment
    repository.payments[latest_failed_payment.id] = latest_failed_payment
    repository.invoices[invoice.id] = invoice
    repository.instruments[instrument.id] = instrument
    repository.billing_methods[method.id] = method
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "provider rejected final billing retry"
    )

    result = await retry_subscription_billing(
        invoice.id,
        BillingRetryCommand(reason="scheduled_retry"),
        repository,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="billing-retry-key",
        operation_locks=test_dependencies.operation_locks,
    )

    final_payment = repository.payments[invoice.payment_id]
    assert result.status == "canceled"
    assert result.invoice_status == "issued"
    assert result.payment_status == "failed"
    assert result.next_billing_date is None
    assert result.notification["template"] == "subscription_canceled_payment_failed"
    payload = result.notification["payload"]
    assert isinstance(payload, dict)
    assert payload["invoiceId"] == invoice.id
    assert payload["cancelReason"] == "payment_retry_exhausted"
    assert payload["subscriptionManageUrl"] == "/subscriptions/me"
    assert payload["resubscribeUrl"] == "/subscriptions/checkout?productCode=basic"
    assert payload["canceledAt"] == "2026-06-10T00:00:00+00:00"
    assert subscription.status == "canceled"
    assert subscription.next_billing_at is None
    assert subscription.canceled_at == datetime(2026, 6, 10, tzinfo=UTC)
    assert final_payment.status == "failed"
    assert final_payment.retry_scheduled_at is None
    assert final_payment.failure is not None
    assert final_payment.failure["retryable"] is False


async def test_retry_subscription_billing_cancels_on_non_retryable_provider_error(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry_nonretryable",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry_nonretryable",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry_nonretryable",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    billing_cycle_key = "sub_retry_nonretryable:2026-06-10T00:00:00+00:00"
    payment = Payment(
        id="pay_retry_nonretryable",
        order_id="ord_retry_nonretryable",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_nonretryable",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry_nonretryable",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry_nonretryable",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name="Hyundai 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice
    repository.instruments[instrument.id] = instrument
    repository.billing_methods[method.id] = method
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "billing key is no longer valid",
        provider_code="INVALID_BILLING_KEY",
        retryable=False,
    )

    result = await retry_subscription_billing(
        invoice.id,
        BillingRetryCommand(reason="scheduled_retry"),
        repository,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="billing-retry-nonretryable-key",
        operation_locks=test_dependencies.operation_locks,
    )

    final_payment = repository.payments[invoice.payment_id]
    assert result.status == "canceled"
    assert result.next_billing_date is None
    assert subscription.status == "canceled"
    assert subscription.next_billing_at is None
    assert final_payment.status == "failed"
    assert final_payment.retry_scheduled_at is None
    assert final_payment.failure is not None
    assert final_payment.failure["providerCode"] == "INVALID_BILLING_KEY"
    assert final_payment.failure["retryable"] is False


async def test_retry_subscription_billing_rejects_missing_retry_schedule(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment = Payment(
        id="pay_retry_unscheduled",
        order_id="ord_retry_unscheduled",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id="sub_retry_unscheduled",
        retry_scheduled_at=None,
    )
    invoice = Invoice(
        id="inv_retry_unscheduled",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id="sub_retry_unscheduled",
    )
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice

    with pytest.raises(InvalidStateTransitionError):
        await retry_subscription_billing(
            invoice.id,
            BillingRetryCommand(reason="scheduled_retry"),
            repository,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            idempotency_key="billing-retry-unscheduled-key",
        )

    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0


async def test_retry_subscription_billing_checks_latest_failed_payment_schedule(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    billing_cycle_key = "sub_retry:2026-06-10T00:00:00+00:00"
    original_payment = Payment(
        id="pay_retry_original",
        order_id="ord_retry_original",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id="sub_retry",
        billing_cycle_key=billing_cycle_key,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    latest_payment = Payment(
        id="pay_retry_latest",
        order_id="ord_retry_latest",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id="sub_retry",
        billing_cycle_key=billing_cycle_key,
        retry_scheduled_at=datetime(2026, 6, 11, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_latest",
        user_id="user_1",
        payment_id=original_payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id="sub_retry",
        billing_cycle_key=billing_cycle_key,
    )
    repository.payments[original_payment.id] = original_payment
    repository.payments[latest_payment.id] = latest_payment
    repository.invoices[invoice.id] = invoice

    with pytest.raises(InvalidStateTransitionError, match="retry is not due"):
        await retry_subscription_billing(
            invoice.id,
            BillingRetryCommand(reason="scheduled_retry"),
            repository,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            idempotency_key="billing-retry-latest-key",
        )

    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0


async def test_retry_subscription_billing_rejects_idempotency_conflict(
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry",
        order_id="ord_retry",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name="Hyundai 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice
    repository.instruments[instrument.id] = instrument
    repository.billing_methods[method.id] = method
    kwargs = {
        "invoice_id": invoice.id,
        "command": BillingRetryCommand(reason="scheduled_retry"),
        "repository": repository,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "billing_key_cipher": test_dependencies.billing_key_cipher,
        "idempotency_key": "billing-retry-key",
    }
    await retry_subscription_billing(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        await retry_subscription_billing(
            **{
                **kwargs,
                "command": BillingRetryCommand(reason="manual_retry", force=True),
            }
        )
