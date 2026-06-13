from __future__ import annotations

import logging
from datetime import UTC, datetime

from payments.application.errors import ProviderError
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.operation_lock import OperationLock
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


def test_internal_billing_run_requires_internal_job_token(client) -> None:
    response = client.post(
        "/internal/subscription-billing/run",
        headers={"X-Request-Id": "req_job"},
        json={"jobType": "cancel_expiration"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_internal_billing_run_requires_request_id(client) -> None:
    response = client.post(
        "/internal/subscription-billing/run",
        headers={"Internal-Job-Token": "secret"},
        json={"jobType": "cancel_expiration"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_internal_billing_run_rejects_schema_validation_as_400(client) -> None:
    invalid_payloads = [
        {"jobType": "billing", "limit": 0},
        {"jobType": "billing", "limit": "100"},
        {"jobType": "billing", "dryRun": "true"},
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/internal/subscription-billing/run",
            headers={
                "Internal-Job-Token": "secret",
                "X-Request-Id": "req_job",
            },
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_internal_billing_retry_rejects_invalid_contract_values(
    client,
) -> None:
    invalid_payloads = [
        {"force": "true"},
        {"dryRun": "true"},
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/internal/subscription-billing/inv_missing/retry",
            headers={
                "Internal-Job-Token": "secret",
                "X-Request-Id": "req_retry",
                "Idempotency-Key": "retry-key",
            },
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_internal_billing_run_expires_cancel_scheduled_subscription(
    client,
    test_dependencies,
    caplog,
) -> None:
    test_dependencies.subscription_expirations.subscriptions["sub_expired"] = (
        Subscription(
            id="sub_expired",
            user_id="user_1",
            payment_customer_id="cust_1",
            plan_id="plan_basic_monthly",
            product_code="basic",
            status="cancel_scheduled",
            cancel_at_period_end=True,
            next_billing_at=None,
            current_period_start_at=datetime(2026, 5, 10, tzinfo=UTC),
            current_period_end_at=datetime(2026, 6, 10, tzinfo=UTC),
        )
    )

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/internal/subscription-billing/run",
            headers={
                "Internal-Job-Token": "secret",
                "X-Request-Id": "req_job",
            },
            json={"jobType": "cancel_expiration", "limit": 100},
        )

    assert response.status_code == 200
    assert response.json() == {
        "jobType": "cancel_expiration",
        "selected": 1,
        "processed": 1,
        "skipped": 0,
        "failed": 0,
        "cancelExpirationEmailsQueued": 1,
        "expiredSubscriptionIds": ["sub_expired"],
    }
    assert (
        test_dependencies.subscription_expirations.subscriptions[
            "sub_expired"
        ].status
        == "canceled"
    )
    assert test_dependencies.operation_locks.acquire_calls == [
        "internal-billing-run:cancel_expiration:2026-06-10"
    ]
    assert test_dependencies.operation_locks.release_calls == [
        "internal-billing-run:cancel_expiration:2026-06-10"
    ]
    assert test_dependencies.subscription_expiration_uow_factory.commit_count == 1
    audit = next(
        iter(test_dependencies.payment_stores.operator_audits.operator_audits.values())
    )
    assert audit.action == "subscription.cancel_expired"
    [record] = [
        record
        for record in caplog.records
        if record.message == "internal_billing_run_completed"
    ]
    assert record.payment_job_type == "cancel_expiration"
    assert record.payment_processed == 1
    assert record.payment_failed == 0
    assert record.payment_skipped == 0


def test_internal_billing_run_charges_due_subscription(
    client,
    test_dependencies,
    caplog,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/internal/subscription-billing/run",
            headers={
                "Internal-Job-Token": "secret",
                "X-Request-Id": "req_job",
                "Idempotency-Key": "billing-run-key",
            },
            json={
                "jobType": "billing",
                "billingDate": "2026-06-10",
                "limit": 100,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "billingDate": "2026-06-10",
        "processed": 1,
        "paid": 1,
        "failed": 0,
        "skipped": 0,
        "excludedCancelScheduled": 0,
        "reminderEmailsSent": 0,
        "successEmailsQueued": 1,
        "failureEmailsQueued": 0,
    }
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1
    invoice = next(iter(test_dependencies.billing_retries.invoices.values()))
    payment = test_dependencies.billing_retries.payments[invoice.payment_id]
    subscription = test_dependencies.billing_retries.subscriptions["sub_due"]
    assert invoice.status == "paid"
    assert payment.status == "paid"
    assert payment.billing_cycle_key == invoice.billing_cycle_key
    assert (
        test_dependencies.payment_provider.last_billing_charge_idempotency_key
        == payment.billing_cycle_key
    )
    assert payment.billing_method_id == "bm_due"
    assert subscription.next_billing_at == datetime(2026, 7, 10, tzinfo=UTC)
    assert test_dependencies.subscription_billing_uow_factory.enter_count == 1
    assert test_dependencies.subscription_billing_uow_factory.commit_count == 1
    assert test_dependencies.subscription_billing_uow_factory.rollback_count == 0
    billing_cycle_lock_key = (
        f"subscription-billing:sub_due:{payment.billing_cycle_key}"
    )
    assert test_dependencies.operation_locks.acquire_calls == [
        "internal-billing-run:billing:2026-06-10",
        billing_cycle_lock_key,
    ]
    assert test_dependencies.operation_locks.release_calls == [
        billing_cycle_lock_key,
        "internal-billing-run:billing:2026-06-10",
    ]
    [record] = [
        record
        for record in caplog.records
        if record.message == "internal_billing_run_completed"
    ]
    assert record.payment_job_type == "billing"
    assert record.payment_processed == 1
    assert record.payment_paid == 1
    assert record.payment_failed == 0
    assert record.payment_excluded_cancel_scheduled == 0


def test_internal_billing_run_preserves_31st_billing_anchor_after_short_month(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(
        test_dependencies,
        next_billing_at=datetime(2026, 2, 28, tzinfo=UTC),
        current_period_start_at=datetime(2026, 1, 31, tzinfo=UTC),
        billing_anchor_day=31,
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
            "Idempotency-Key": "billing-run-31st-anchor-key",
        },
        json={
            "jobType": "billing",
            "billingDate": "2026-02-28",
            "limit": 100,
        },
    )

    assert response.status_code == 200
    subscription = test_dependencies.billing_retries.subscriptions["sub_due"]
    assert subscription.next_billing_at == datetime(2026, 3, 31, tzinfo=UTC)


def test_internal_billing_run_preserves_30th_billing_anchor_after_short_month(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(
        test_dependencies,
        next_billing_at=datetime(2026, 2, 28, tzinfo=UTC),
        current_period_start_at=datetime(2026, 1, 30, tzinfo=UTC),
        billing_anchor_day=30,
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
            "Idempotency-Key": "billing-run-30th-anchor-key",
        },
        json={
            "jobType": "billing",
            "billingDate": "2026-02-28",
            "limit": 100,
        },
    )

    assert response.status_code == 200
    subscription = test_dependencies.billing_retries.subscriptions["sub_due"]
    assert subscription.next_billing_at == datetime(2026, 3, 30, tzinfo=UTC)


def test_internal_billing_run_skips_locked_billing_cycle(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    billing_cycle_key = Payment.generate_billing_cycle_key(
        "sub_due",
        datetime(2026, 6, 10, tzinfo=UTC),
    )
    assert billing_cycle_key is not None
    lock_key = f"subscription-billing:sub_due:{billing_cycle_key}"
    test_dependencies.operation_locks.operation_locks[lock_key] = OperationLock(
        id="lock_existing",
        lock_key=lock_key,
        owner_token="worker_existing",
        fencing_token=1,
        fencing_counter_key="subscription-billing",
        status="active",
        locked_until_at=datetime(2026, 6, 10, 0, 5, tzinfo=UTC),
        acquired_at=datetime(2026, 6, 10, tzinfo=UTC),
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
            "Idempotency-Key": "billing-run-locked-cycle-key",
        },
        json={"jobType": "billing", "billingDate": "2026-06-10", "limit": 100},
    )

    assert response.status_code == 200
    assert response.json() == {
        "billingDate": "2026-06-10",
        "processed": 1,
        "paid": 0,
        "failed": 0,
        "skipped": 1,
        "excludedCancelScheduled": 0,
        "reminderEmailsSent": 0,
        "successEmailsQueued": 0,
        "failureEmailsQueued": 0,
    }
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0
    assert test_dependencies.billing_retries.invoices == {}
    assert test_dependencies.billing_retries.payments == {}
    assert test_dependencies.operation_locks.acquire_calls == [
        "internal-billing-run:billing:2026-06-10",
        lock_key,
    ]
    assert test_dependencies.operation_locks.release_calls == [
        "internal-billing-run:billing:2026-06-10",
    ]


def test_internal_billing_run_accepts_missing_body_defaults(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
            "Idempotency-Key": "billing-run-default-key",
        },
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert response.json()["paid"] == 1
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


def test_internal_billing_run_skips_subscription_canceled_after_selection(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    repository = test_dependencies.billing_retries
    original_list_due = repository.list_due_active_subscriptions

    async def list_due_then_cancel(billing_cutoff_at, limit):
        subscriptions = await original_list_due(billing_cutoff_at, limit)
        subscription = repository.subscriptions["sub_due"]
        subscription.status = "cancel_scheduled"
        subscription.cancel_at_period_end = True
        subscription.next_billing_at = None
        return subscriptions

    repository.list_due_active_subscriptions = list_due_then_cancel

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
            "Idempotency-Key": "billing-run-race-key",
        },
        json={"jobType": "billing", "billingDate": "2026-06-10", "limit": 100},
    )

    assert response.status_code == 200
    assert response.json() == {
        "billingDate": "2026-06-10",
        "processed": 1,
        "paid": 0,
        "failed": 0,
        "skipped": 1,
        "excludedCancelScheduled": 1,
        "reminderEmailsSent": 0,
        "successEmailsQueued": 0,
        "failureEmailsQueued": 0,
    }
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0
    assert repository.invoices == {}
    assert repository.payments == {}
    assert repository.subscriptions["sub_due"].status == "cancel_scheduled"


def test_internal_billing_run_applies_pending_plan_before_charge(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    repository = test_dependencies.billing_retries
    subscription = repository.subscriptions["sub_due"]
    subscription.plan_id = "plan_pro_monthly"
    subscription.pending_plan_id = "plan_due_monthly"
    subscription.pending_plan_effective_at = datetime(2026, 6, 10, tzinfo=UTC)
    repository.subscription_plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id="product_basic",
        plan_code="PRO_MONTHLY",
        billing_period="monthly",
        amount=19_900,
        entitlements={},
        status="active",
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
        },
        json={"jobType": "billing", "billingDate": "2026-06-10", "limit": 100},
    )

    invoice = next(iter(repository.invoices.values()))
    payment = repository.payments[invoice.payment_id]
    assert response.status_code == 200
    assert response.json()["paid"] == 1
    assert payment.amount == 9900
    saved_subscription = repository.subscriptions["sub_due"]
    assert saved_subscription.plan_id == "plan_due_monthly"
    assert saved_subscription.pending_plan_id is None
    assert saved_subscription.pending_plan_effective_at is None


def test_internal_billing_run_reuses_idempotent_response(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    headers = {
        "Internal-Job-Token": "secret",
        "X-Request-Id": "req_job",
        "Idempotency-Key": "billing-run-key",
    }
    payload = {"jobType": "billing", "billingDate": "2026-06-10", "limit": 100}

    first = client.post(
        "/internal/subscription-billing/run",
        headers=headers,
        json=payload,
    )
    second = client.post(
        "/internal/subscription-billing/run",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


def test_internal_billing_run_idempotency_conflict_returns_409(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    headers = {
        "Internal-Job-Token": "secret",
        "X-Request-Id": "req_job",
        "Idempotency-Key": "billing-run-key",
    }

    first = client.post(
        "/internal/subscription-billing/run",
        headers=headers,
        json={"jobType": "billing", "billingDate": "2026-06-10", "limit": 100},
    )
    second = client.post(
        "/internal/subscription-billing/run",
        headers=headers,
        json={"jobType": "billing", "billingDate": "2026-06-11", "limit": 100},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_internal_billing_run_dry_run_does_not_charge(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
        },
        json={"jobType": "billing", "billingDate": "2026-06-10", "dryRun": True},
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert response.json()["paid"] == 0
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0
    assert test_dependencies.billing_retries.invoices == {}


def test_internal_billing_run_records_documented_failure_snapshot(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "provider billing charge failed",
        provider_code="REJECT_CARD_COMPANY",
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
        },
        json={"jobType": "billing", "billingDate": "2026-06-10"},
    )

    assert response.status_code == 200
    assert response.json()["failed"] == 1
    invoice = next(iter(test_dependencies.billing_retries.invoices.values()))
    payment = test_dependencies.billing_retries.payments[invoice.payment_id]
    subscription = test_dependencies.billing_retries.subscriptions["sub_due"]
    assert invoice.status == "issued"
    assert subscription.status == "past_due"
    assert payment.status == "failed"
    assert payment.retry_scheduled_at == datetime(2026, 6, 11, tzinfo=UTC)
    assert payment.failure == {
        "phase": "confirm",
        "reason": "provider_rejected",
        "providerCode": "REJECT_CARD_COMPANY",
        "message": "provider billing charge failed",
        "retryable": True,
    }


def test_internal_billing_run_snapshots_billing_method_for_revoked_instrument(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    repository = test_dependencies.billing_retries
    repository.instruments["pinstr_due"].status = "revoked"

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
        },
        json={"jobType": "billing", "billingDate": "2026-06-10"},
    )

    assert response.status_code == 200
    assert response.json()["failed"] == 1
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0
    invoice = next(iter(repository.invoices.values()))
    payment = repository.payments[invoice.payment_id]
    assert payment.status == "failed"
    assert payment.billing_method_id == "bm_due"
    assert payment.failure == {
        "phase": "confirm",
        "reason": "provider_error",
        "providerCode": "BILLING_METHOD_NOT_CHARGEABLE",
        "message": "default billing method is not chargeable",
        "retryable": False,
    }
    assert payment.retry_scheduled_at is None


def test_internal_billing_run_does_not_schedule_non_retryable_provider_failure(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(test_dependencies)
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "billing key is no longer valid",
        provider_code="INVALID_BILLING_KEY",
        retryable=False,
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
        },
        json={"jobType": "billing", "billingDate": "2026-06-10"},
    )

    assert response.status_code == 200
    assert response.json()["failed"] == 1
    invoice = next(iter(test_dependencies.billing_retries.invoices.values()))
    payment = test_dependencies.billing_retries.payments[invoice.payment_id]
    assert payment.status == "failed"
    assert payment.retry_scheduled_at is None
    assert payment.failure == {
        "phase": "confirm",
        "reason": "provider_rejected",
        "providerCode": "INVALID_BILLING_KEY",
        "message": "billing key is no longer valid",
        "retryable": False,
    }


def test_internal_billing_run_sends_reminders_without_charge(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(
        test_dependencies,
        next_billing_at=datetime(2026, 6, 17, tzinfo=UTC),
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
        },
        json={"jobType": "reminder", "billingDate": "2026-06-10"},
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert response.json()["reminderEmailsSent"] == 1
    assert response.json()["paid"] == 0
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0
    stored_keys = (
        test_dependencies.payment_stores.idempotency_keys.idempotency_keys.values()
    )
    reminder_key = next(
        key
        for key in stored_keys
        if key.scope == "subscription-billing-reminder"
    )
    assert reminder_key.resource_type == "subscription_billing_reminder"
    assert reminder_key.resource_id == "sub_due"
    assert reminder_key.response_body is not None
    assert reminder_key.response_body["notification"] == {
        "template": "subscription_billing_reminder",
        "payload": {
            "subscriptionId": "sub_due",
            "userId": "user_1",
            "billingDate": "2026-06-17",
            "amount": 9900,
            "currency": "KRW",
            "planName": "Basic 월간",
            "subscriptionManageUrl": "/subscriptions/me",
        },
    }
    assert reminder_key.response_body["reminderSentAt"] == (
        test_dependencies.clock.utc_now()
    )


def test_internal_billing_run_skips_duplicate_reminder_history(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(
        test_dependencies,
        next_billing_at=datetime(2026, 6, 17, tzinfo=UTC),
    )
    payload = {"jobType": "reminder", "billingDate": "2026-06-10"}
    headers = {
        "Internal-Job-Token": "secret",
        "X-Request-Id": "req_job",
    }

    first = client.post(
        "/internal/subscription-billing/run",
        headers=headers,
        json=payload,
    )
    second = client.post(
        "/internal/subscription-billing/run",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["reminderEmailsSent"] == 1
    assert second.json()["processed"] == 1
    assert second.json()["skipped"] == 1
    assert second.json()["reminderEmailsSent"] == 0
    stored_keys = (
        test_dependencies.payment_stores.idempotency_keys.idempotency_keys.values()
    )
    reminder_keys = [
        key
        for key in stored_keys
        if key.scope == "subscription-billing-reminder"
    ]
    assert len(reminder_keys) == 1


def test_internal_billing_run_reminder_dry_run_does_not_send(
    client,
    test_dependencies,
) -> None:
    _prepare_due_subscription_billing(
        test_dependencies,
        next_billing_at=datetime(2026, 6, 17, tzinfo=UTC),
    )

    response = client.post(
        "/internal/subscription-billing/run",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_job",
        },
        json={
            "jobType": "reminder",
            "billingDate": "2026-06-10",
            "dryRun": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert response.json()["reminderEmailsSent"] == 0
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0


def test_retry_subscription_billing_returns_paid(client, test_dependencies) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry_route",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry_route",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry_route",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry_route",
        order_id="ord_retry_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_route",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry_route",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry_route",
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

    response = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_retry",
            "Idempotency-Key": "retry-key",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["invoiceId"] == invoice.id
    assert body["status"] == "active"
    assert body["invoiceStatus"] == "paid"
    assert body["paymentStatus"] == "paid"
    assert body["receiptUrl"] == "https://dashboard.tosspayments.com/receipt/billing"
    assert body["notification"]["payload"]["invoiceId"] == invoice.id
    assert repository.payments[invoice.payment_id].billing_method_id == method.id
    assert test_dependencies.subscription_billing_uow_factory.enter_count == 1
    assert test_dependencies.subscription_billing_uow_factory.commit_count == 1


def test_retry_subscription_billing_requires_idempotency_key(
    client,
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    subscription = Subscription(
        id="sub_retry_route",
        user_id="user_1",
        payment_customer_id="pcus_retry_route",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry_route",
        order_id="ord_retry_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_route",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice

    response = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_retry",
        },
        json={"reason": "scheduled_retry"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_retry_subscription_billing_requires_request_id(client) -> None:
    response = client.post(
        "/internal/subscription-billing/inv_retry_route/retry",
        headers={
            "Internal-Job-Token": "secret",
            "Idempotency-Key": "retry-key",
        },
        json={"reason": "scheduled_retry"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_or_invalid_request_context"


def test_retry_subscription_billing_returns_failed_retry_result(
    client,
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry_route",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry_route",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry_route",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry_route",
        order_id="ord_retry_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_route",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry_route",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry_route",
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
        "provider rejected billing retry"
    )

    response = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_retry",
            "Idempotency-Key": "retry-key",
        },
        json={"reason": "scheduled_retry"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "past_due"
    assert body["invoiceStatus"] == "issued"
    assert body["paymentStatus"] == "failed"
    assert body["nextBillingDate"] == "2026-06-11"
    assert body["notification"]["template"] == "subscription_payment_failed"
    assert body["notification"]["payload"]["invoiceId"] == invoice.id
    assert body["notification"]["payload"]["retryScheduledAt"] == "2026-06-11"
    assert repository.payments[invoice.payment_id].billing_method_id == method.id


def test_retry_subscription_billing_cancels_subscription_after_final_failure(
    client,
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry_route",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry_route",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry_route",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    billing_cycle_key = "sub_retry_route:2026-06-10T00:00:00+00:00"
    first_payment = Payment(
        id="pay_retry_first_route",
        order_id="ord_retry_first_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 8, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    latest_payment = Payment(
        id="pay_retry_latest_route",
        order_id="ord_retry_latest_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_route",
        user_id="user_1",
        payment_id=latest_payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 9, tzinfo=UTC),
        subscription_id=subscription.id,
        billing_cycle_key=billing_cycle_key,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry_route",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry_route",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id=instrument.id,
        display_name="Hyundai 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.subscriptions[subscription.id] = subscription
    repository.payments[first_payment.id] = first_payment
    repository.payments[latest_payment.id] = latest_payment
    repository.invoices[invoice.id] = invoice
    repository.instruments[instrument.id] = instrument
    repository.billing_methods[method.id] = method
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "provider rejected final billing retry"
    )

    response = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_retry",
            "Idempotency-Key": "retry-final-key",
        },
        json={"reason": "scheduled_retry"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "canceled"
    assert body["invoiceStatus"] == "issued"
    assert body["paymentStatus"] == "failed"
    assert body["nextBillingDate"] is None
    assert body["notification"]["template"] == "subscription_canceled_payment_failed"
    assert (
        body["notification"]["payload"]["cancelReason"]
        == "payment_retry_exhausted"
    )
    assert body["notification"]["payload"]["subscriptionManageUrl"] == (
        "/subscriptions/me"
    )
    assert repository.subscriptions[subscription.id].status == "canceled"
    assert repository.subscriptions[subscription.id].next_billing_at is None
    assert repository.payments[invoice.payment_id].retry_scheduled_at is None


def test_retry_subscription_billing_rejects_missing_retry_schedule(
    client,
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment = Payment(
        id="pay_retry_unscheduled_route",
        order_id="ord_retry_unscheduled_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id="sub_retry_unscheduled_route",
        retry_scheduled_at=None,
    )
    invoice = Invoice(
        id="inv_retry_unscheduled_route",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id="sub_retry_unscheduled_route",
    )
    repository.payments[payment.id] = payment
    repository.invoices[invoice.id] = invoice

    response = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers={
            "Internal-Job-Token": "secret",
            "X-Request-Id": "req_retry",
            "Idempotency-Key": "retry-unscheduled-key",
        },
        json={"reason": "scheduled_retry"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0


def test_retry_subscription_billing_reuses_idempotent_response(
    client,
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry_route",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry_route",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry_route",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry_route",
        order_id="ord_retry_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_route",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry_route",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry_route",
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
    headers = {
        "Internal-Job-Token": "secret",
        "X-Request-Id": "req_retry",
        "Idempotency-Key": "retry-key",
    }

    first = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers=headers,
        json={"reason": "scheduled_retry"},
    )
    second = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers=headers,
        json={"reason": "scheduled_retry"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


def test_retry_subscription_billing_idempotency_conflict_returns_409(
    client,
    test_dependencies,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_retry_route",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_retry_route",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription = Subscription(
        id="sub_retry_route",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="past_due",
        cancel_at_period_end=False,
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    payment = Payment(
        id="pay_retry_route",
        order_id="ord_retry_route",
        amount=9_900,
        status="failed",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
        retry_scheduled_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    invoice = Invoice(
        id="inv_retry_route",
        user_id="user_1",
        payment_id=payment.id,
        status="issued",
        issued_at=datetime(2026, 6, 10, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    instrument = PaymentInstrument(
        id="pinstr_retry_route",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    method = BillingMethod(
        id="bm_retry_route",
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
    headers = {
        "Internal-Job-Token": "secret",
        "X-Request-Id": "req_retry",
        "Idempotency-Key": "retry-key",
    }
    first = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers=headers,
        json={"reason": "scheduled_retry"},
    )
    second = client.post(
        f"/internal/subscription-billing/{invoice.id}/retry",
        headers=headers,
        json={"reason": "manual_retry", "force": True},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def _prepare_due_subscription_billing(
    test_dependencies,
    *,
    next_billing_at: datetime = datetime(2026, 6, 10, tzinfo=UTC),
    current_period_start_at: datetime = datetime(2026, 5, 10, tzinfo=UTC),
    billing_anchor_day: int | None = None,
) -> None:
    repository = test_dependencies.billing_retries
    payment_customer = PaymentCustomer(
        id="pcus_due",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_due",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    subscription_kwargs = {}
    if billing_anchor_day is not None:
        subscription_kwargs["billing_anchor_day"] = billing_anchor_day
    repository.subscriptions["sub_due"] = Subscription(
        id="sub_due",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_due_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        next_billing_at=next_billing_at,
        current_period_start_at=current_period_start_at,
        current_period_end_at=next_billing_at,
        **subscription_kwargs,
    )
    repository.subscription_plans["plan_due_monthly"] = SubscriptionPlan(
        id="plan_due_monthly",
        product_id="product_basic",
        plan_code="BASIC_MONTHLY",
        billing_period="monthly",
        amount=9900,
        entitlements={},
        status="active",
    )
    repository.billing_methods["bm_due"] = BillingMethod(
        id="bm_due",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id="pinstr_due",
        display_name="현대카드 **** 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    repository.instruments["pinstr_due"] = PaymentInstrument(
        id="pinstr_due",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_due"),
        billing_key_hash="hash_due",
        status="active",
    )
