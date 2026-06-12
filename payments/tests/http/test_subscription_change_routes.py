from __future__ import annotations

from datetime import UTC, datetime

from payments.application.errors import ProviderError
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


def test_create_subscription_change_preview_returns_upgrade_preview(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=14900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    response = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={"targetPlanId": "plan_pro_monthly"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionId"] == "sub_123"
    assert body["currentPlanId"] == "plan_basic_monthly"
    assert body["targetPlanId"] == "plan_pro_monthly"
    assert body["serverDecision"] == "upgrade"
    assert body["willApply"] == "immediate"
    assert body["nextBillingDate"] == "2026-07-01"
    assert body["immediatePayment"] == {
        "amount": 3500,
        "currency": "KRW",
        "invoiceType": "plan_change",
    }
    assert body["notice"] == (
        "업그레이드는 확인 즉시 3,500원이 결제되고 플랜이 바로 변경됩니다. "
        "다음 결제일은 2026-07-01입니다."
    )
    assert body["confirmationToken"].startswith("pct_")


def test_create_subscription_change_preview_reuses_idempotency_header(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=14900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    headers = {**auth_headers, "Idempotency-Key": "preview-key"}

    first = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=headers,
        json={"targetPlanId": "plan_pro_monthly"},
    )
    second = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=headers,
        json={"targetPlanId": "plan_pro_monthly"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(test_dependencies.payment_stores.idempotency_keys.idempotency_keys) == 1


def test_create_subscription_change_preview_idempotency_conflict_returns_409(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=14900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.catalog_repository.plans["plan_enterprise_monthly"] = (
        SubscriptionPlan(
            id="plan_enterprise_monthly",
            product_id=product.id,
            plan_code="enterprise_monthly",
            billing_period="monthly",
            amount=29900,
            entitlements={"seats": 10},
            status="active",
        )
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    headers = {**auth_headers, "Idempotency-Key": "preview-key"}

    first = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=headers,
        json={"targetPlanId": "plan_pro_monthly"},
    )
    second = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=headers,
        json={"targetPlanId": "plan_enterprise_monthly"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_create_subscription_change_preview_rejects_billing_date_override(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=14900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    response = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={
            "targetPlanId": "plan_pro_monthly",
            "billingDate": "2026-07-15",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert test_dependencies.payment_stores.idempotency_keys.idempotency_keys == {}


def test_create_subscription_change_preview_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    invalid_payloads = [{}, {"targetPlanId": 123}, {"targetPlanId": ""}]

    for payload in invalid_payloads:
        response = client.post(
            "/subscriptions/sub_123/change-preview",
            headers=auth_headers,
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_create_subscription_change_preview_returns_403_for_other_user(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=14900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_2",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    response = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={"targetPlanId": "plan_pro_monthly"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_execute_subscription_change_schedules_downgrade(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=19_900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_pro_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={"targetPlanId": "plan_basic_monthly"},
    )

    response = client.patch(
        "/subscriptions/sub_123",
        headers={**auth_headers, "Idempotency-Key": "subscription-change-key"},
        json={
            "confirmationToken": preview.json()["confirmationToken"],
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionId"] == "sub_123"
    assert body["serverDecision"] == "downgrade"
    assert body["planId"] == "plan_pro_monthly"
    assert body["nextBillingDate"] == "2026-07-01"
    assert body["payment"] is None
    assert body["pendingPlan"] == {
        "planId": "plan_basic_monthly",
        "planName": "Basic monthly",
        "effectiveAt": "2026-07-01",
    }


def test_execute_subscription_change_rejects_billing_date_override(
    client,
    auth_headers,
) -> None:
    response = client.patch(
        "/subscriptions/sub_123",
        headers={**auth_headers, "Idempotency-Key": "subscription-change-key"},
        json={
            "confirmationToken": "pct_123",
            "confirmed": True,
            "nextBillingDate": "2026-07-15",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_execute_subscription_change_returns_402_on_upgrade_payment_failure(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    test_dependencies.payment_stores.payment_customers.payment_customers[
        payment_customer.id
    ] = payment_customer
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=14_900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    test_dependencies.billing_retries.billing_methods["bm_1"] = BillingMethod(
        id="bm_1",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        instrument_id="pinstr_1",
        display_name="현대 **** 1234",
        provider="tosspayments",
        is_default=True,
        status="active",
    )
    test_dependencies.billing_retries.instruments["pinstr_1"] = PaymentInstrument(
        id="pinstr_1",
        payment_customer_id=payment_customer.id,
        provider="tosspayments",
        billing_key=test_dependencies.billing_key_cipher.encrypt("billing_key_secret"),
        billing_key_hash="hash",
        status="active",
    )
    preview = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={"targetPlanId": "plan_pro_monthly"},
    )
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "card company rejected plan change",
        provider_code="REJECT_CARD_COMPANY",
    )

    response = client.patch(
        "/subscriptions/sub_123",
        headers={**auth_headers, "Idempotency-Key": "subscription-change-key"},
        json={
            "confirmationToken": preview.json()["confirmationToken"],
            "confirmed": True,
        },
    )

    body = response.json()
    subscription = test_dependencies.subscription_accounts.subscriptions["sub_123"]
    assert response.status_code == 402
    assert body["subscriptionId"] == "sub_123"
    assert body["planId"] == "plan_basic_monthly"
    assert body["payment"]["status"] == "failed"
    assert body["payment"]["failure"]["message"] == (
        "card company rejected plan change"
    )
    assert body["payment"]["failure"]["providerCode"] == "REJECT_CARD_COMPANY"
    assert body["payment"]["failure"]["reason"] == "provider_rejected"
    assert subscription.plan_id == "plan_basic_monthly"


def test_execute_subscription_change_requires_idempotency_key(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=19_900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_pro_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={"targetPlanId": "plan_basic_monthly"},
    )

    response = client.patch(
        "/subscriptions/sub_123",
        headers=auth_headers,
        json={
            "confirmationToken": preview.json()["confirmationToken"],
            "confirmed": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_execute_subscription_change_rejects_invalid_contract_values_as_400(
    client,
    auth_headers,
) -> None:
    invalid_payloads = [
        {},
        {"confirmationToken": 123, "confirmed": True},
        {"confirmationToken": "pct_123", "confirmed": False},
        {"confirmationToken": "pct_123", "confirmed": "true"},
        {"confirmationToken": "", "confirmed": True},
    ]

    for index, payload in enumerate(invalid_payloads):
        response = client.patch(
            "/subscriptions/sub_123",
            headers={
                **auth_headers,
                "Idempotency-Key": f"subscription-change-invalid-{index}",
            },
            json=payload,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"


def test_execute_subscription_change_reuses_idempotent_response(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=19_900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_pro_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={"targetPlanId": "plan_basic_monthly"},
    )
    headers = {**auth_headers, "Idempotency-Key": "subscription-change-key"}
    payload = {
        "confirmationToken": preview.json()["confirmationToken"],
        "confirmed": True,
    }

    first = client.patch("/subscriptions/sub_123", headers=headers, json=payload)
    second = client.patch("/subscriptions/sub_123", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


def test_execute_subscription_change_idempotency_conflict_returns_409(
    client,
    auth_headers,
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_pro_monthly"] = SubscriptionPlan(
        id="plan_pro_monthly",
        product_id=product.id,
        plan_code="pro_monthly",
        billing_period="monthly",
        amount=19_900,
        entitlements={"seats": 5},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_pro_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = client.post(
        "/subscriptions/sub_123/change-preview",
        headers=auth_headers,
        json={"targetPlanId": "plan_basic_monthly"},
    )
    headers = {**auth_headers, "Idempotency-Key": "subscription-change-key"}
    first = client.patch(
        "/subscriptions/sub_123",
        headers=headers,
        json={
            "confirmationToken": preview.json()["confirmationToken"],
            "confirmed": True,
        },
    )
    second = client.patch(
        "/subscriptions/sub_123",
        headers=headers,
        json={
            "confirmationToken": "pct_other",
            "confirmed": True,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"
