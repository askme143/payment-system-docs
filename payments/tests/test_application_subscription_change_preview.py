from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from payments.application.context import RequestContext
from payments.application.errors import (
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentRequiredResponseError,
    ProviderError,
)
from payments.application.ports.provider import BillingChargeProviderResult
from payments.application.subscription_changes import (
    SubscriptionChangeCommand,
    SubscriptionChangePreviewCommand,
    create_subscription_change_preview,
    execute_subscription_change,
)
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def utc_now(self) -> datetime:
        return self.now


async def test_create_subscription_change_preview_calculates_upgrade(
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

    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )

    assert preview.subscription_id == "sub_123"
    assert preview.server_decision == "upgrade"
    assert preview.will_apply == "immediate"
    assert preview.immediate_payment == {
        "amount": 3500,
        "currency": "KRW",
        "invoiceType": "plan_change",
    }
    assert preview.next_billing_date == datetime(2026, 7, 1, tzinfo=UTC)
    assert preview.notice == (
        "업그레이드는 확인 즉시 3,500원이 결제되고 플랜이 바로 변경됩니다. "
        "다음 결제일은 2026-07-01입니다."
    )
    assert preview.confirmation_token.startswith("pct_")
    decoded = test_dependencies.subscription_change_tokens.decode_plan_change_preview(
        preview.confirmation_token,
    )
    assert decoded is not None
    assert decoded.target_plan_id == "plan_pro_monthly"


async def test_create_subscription_change_preview_replays_same_idempotency_key(
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
    ctx = RequestContext(request_id="req_1", user_id="user_1")
    clock = MutableClock(datetime(2026, 6, 10, 0, 0, tzinfo=UTC))

    first = await create_subscription_change_preview(
        ctx,
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        clock,
        test_dependencies.payment_stores.idempotency_keys,
        idempotency_key="preview-key",
    )
    clock.now = clock.now + timedelta(minutes=1)
    second = await create_subscription_change_preview(
        ctx,
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        clock,
        test_dependencies.payment_stores.idempotency_keys,
        idempotency_key="preview-key",
    )

    assert second == first
    assert len(test_dependencies.payment_stores.idempotency_keys.idempotency_keys) == 1


async def test_create_subscription_change_preview_rejects_idempotency_conflict(
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
    ctx = RequestContext(request_id="req_1", user_id="user_1")

    await create_subscription_change_preview(
        ctx,
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
        test_dependencies.payment_stores.idempotency_keys,
        idempotency_key="preview-key",
    )

    with pytest.raises(IdempotencyConflictError):
        await create_subscription_change_preview(
            ctx,
            "sub_123",
            SubscriptionChangePreviewCommand(
                target_plan_id="plan_enterprise_monthly",
            ),
            test_dependencies.subscription_accounts,
            test_dependencies.catalog_repository,
            test_dependencies.subscription_change_tokens,
            test_dependencies.clock,
            test_dependencies.payment_stores.idempotency_keys,
            idempotency_key="preview-key",
        )


async def test_create_subscription_change_preview_rejects_other_product(
    test_dependencies,
) -> None:
    other_product = Product(
        id="product_other",
        product_code="other",
        product_type="subscription",
        name="Other",
        status="active",
    )
    test_dependencies.catalog_repository.products[other_product.id] = other_product
    test_dependencies.catalog_repository.plans["plan_other"] = SubscriptionPlan(
        id="plan_other",
        product_id=other_product.id,
        plan_code="other_monthly",
        billing_period="monthly",
        amount=19900,
        entitlements={},
        status="active",
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status="active",
        cancel_at_period_end=False,
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    import pytest

    from payments.application.errors import InvalidStateTransitionError

    with pytest.raises(InvalidStateTransitionError):
        await create_subscription_change_preview(
            RequestContext(request_id="req_1", user_id="user_1"),
            "sub_123",
            SubscriptionChangePreviewCommand(target_plan_id="plan_other"),
            test_dependencies.subscription_accounts,
            test_dependencies.catalog_repository,
            test_dependencies.subscription_change_tokens,
            test_dependencies.clock,
        )


async def test_create_subscription_change_preview_rejects_other_user_subscription(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    with pytest.raises(ForbiddenError):
        await create_subscription_change_preview(
            RequestContext(request_id="req_1", user_id="user_1"),
            "sub_123",
            SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
            test_dependencies.subscription_accounts,
            test_dependencies.catalog_repository,
            test_dependencies.subscription_change_tokens,
            test_dependencies.clock,
        )


async def test_execute_subscription_change_schedules_downgrade(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_basic_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )

    result = await execute_subscription_change(
        RequestContext(request_id="req_2", user_id="user_1"),
        "sub_123",
        SubscriptionChangeCommand(
            confirmation_token=preview.confirmation_token,
            confirmed=True,
        ),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.billing_retries,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="subscription-change-key",
        operation_locks=test_dependencies.operation_locks,
        subscription_change_uow_factory=(
            test_dependencies.subscription_change_uow_factory
        ),
    )

    subscription = test_dependencies.subscription_accounts.subscriptions["sub_123"]
    audit = next(
        iter(test_dependencies.payment_stores.operator_audits.operator_audits.values())
    )
    assert result.subscription_id == "sub_123"
    assert result.server_decision == "downgrade"
    assert result.plan_id == "plan_pro_monthly"
    assert result.previous_plan_id == "plan_pro_monthly"
    assert result.payment is None
    assert result.pending_plan == {
        "planId": "plan_basic_monthly",
        "planName": "Basic monthly",
        "effectiveAt": datetime(2026, 7, 1, tzinfo=UTC),
    }
    assert subscription.pending_plan_id == "plan_basic_monthly"
    assert test_dependencies.operation_locks.acquire_calls == ["subscription:sub_123"]
    assert test_dependencies.operation_locks.release_calls == ["subscription:sub_123"]
    assert test_dependencies.subscription_change_uow_factory.commit_count == 1
    assert audit.action == "subscription.plan_change"
    assert audit.operator_id == "user_1"
    assert audit.previous_state["plan_id"] == "plan_pro_monthly"
    assert audit.next_state["target_plan_id"] == "plan_basic_monthly"
    assert audit.next_state["pending_plan"] == result.pending_plan


async def test_create_subscription_change_preview_returns_downgrade_notice(
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
    test_dependencies.catalog_repository.plans["plan_enterprise_monthly"] = (
        SubscriptionPlan(
            id="plan_enterprise_monthly",
            product_id=product.id,
            plan_code="enterprise_monthly",
            billing_period="monthly",
            amount=19_900,
            entitlements={"seats": 10},
            status="active",
        )
    )
    test_dependencies.subscription_accounts.subscriptions["sub_123"] = Subscription(
        id="sub_123",
        user_id="user_1",
        payment_customer_id="pcus_1",
        plan_id="plan_enterprise_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_basic_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )

    assert preview.server_decision == "downgrade"
    assert preview.notice == (
        "다운그레이드는 다음 결제일인 2026-07-01에 변경됩니다. "
        "현재 결제 기간에는 기존 플랜 권한이 유지됩니다."
    )


async def test_execute_subscription_change_replays_downgrade_idempotency(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_basic_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )
    kwargs = {
        "requester": RequestContext(request_id="req_2", user_id="user_1"),
        "subscription_id": "sub_123",
        "command": SubscriptionChangeCommand(
            confirmation_token=preview.confirmation_token,
            confirmed=True,
        ),
        "subscriptions": test_dependencies.subscription_accounts,
        "catalog": test_dependencies.catalog_repository,
        "token_codec": test_dependencies.subscription_change_tokens,
        "billing_repository": test_dependencies.billing_retries,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "billing_key_cipher": test_dependencies.billing_key_cipher,
        "idempotency_key": "subscription-change-key",
    }

    first = await execute_subscription_change(**kwargs)
    second = await execute_subscription_change(**kwargs)

    assert second == first


async def test_execute_subscription_change_replays_same_confirmation_token(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_basic_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )
    command = SubscriptionChangeCommand(
        confirmation_token=preview.confirmation_token,
        confirmed=True,
    )

    first = await execute_subscription_change(
        RequestContext(request_id="req_2", user_id="user_1"),
        "sub_123",
        command,
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.billing_retries,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="subscription-change-key",
        operation_locks=test_dependencies.operation_locks,
        subscription_change_uow_factory=(
            test_dependencies.subscription_change_uow_factory
        ),
    )
    second = await execute_subscription_change(
        RequestContext(request_id="req_3", user_id="user_1"),
        "sub_123",
        command,
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.billing_retries,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="subscription-change-key-retry",
        operation_locks=test_dependencies.operation_locks,
        subscription_change_uow_factory=(
            test_dependencies.subscription_change_uow_factory
        ),
    )

    assert second == first
    assert test_dependencies.subscription_change_uow_factory.commit_count == 1
    assert len(test_dependencies.payment_stores.operator_audits.operator_audits) == 1
    assert test_dependencies.operation_locks.acquire_calls == ["subscription:sub_123"]


async def test_execute_subscription_change_rejects_idempotency_conflict(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_basic_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )
    kwargs = {
        "requester": RequestContext(request_id="req_2", user_id="user_1"),
        "subscription_id": "sub_123",
        "command": SubscriptionChangeCommand(
            confirmation_token=preview.confirmation_token,
            confirmed=True,
        ),
        "subscriptions": test_dependencies.subscription_accounts,
        "catalog": test_dependencies.catalog_repository,
        "token_codec": test_dependencies.subscription_change_tokens,
        "billing_repository": test_dependencies.billing_retries,
        "payment_customers": test_dependencies.payment_stores.payment_customers,
        "idempotency_keys": test_dependencies.payment_stores.idempotency_keys,
        "provider": test_dependencies.payment_provider,
        "clock": test_dependencies.clock,
        "billing_key_cipher": test_dependencies.billing_key_cipher,
        "idempotency_key": "subscription-change-key",
    }
    await execute_subscription_change(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        await execute_subscription_change(
            **{
                **kwargs,
                "command": SubscriptionChangeCommand(
                    confirmation_token=preview.confirmation_token,
                    confirmed=False,
                ),
            }
        )


async def test_execute_subscription_change_rejects_stale_preview_price(
    test_dependencies,
) -> None:
    product = test_dependencies.catalog_repository.product
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
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code=product.product_code,
        status="active",
        cancel_at_period_end=False,
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )
    test_dependencies.catalog_repository.plans["plan_pro_monthly"].amount = 16_900

    with pytest.raises(InvalidStateTransitionError):
        await execute_subscription_change(
            RequestContext(request_id="req_2", user_id="user_1"),
            "sub_123",
            SubscriptionChangeCommand(
                confirmation_token=preview.confirmation_token,
                confirmed=True,
            ),
            test_dependencies.subscription_accounts,
            test_dependencies.catalog_repository,
            test_dependencies.subscription_change_tokens,
            test_dependencies.billing_retries,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            idempotency_key="subscription-change-key",
            operation_locks=test_dependencies.operation_locks,
            subscription_change_uow_factory=(
                test_dependencies.subscription_change_uow_factory
            ),
        )

    assert test_dependencies.payment_provider.charge_billing_key_call_count == 0
    assert test_dependencies.payment_stores.idempotency_keys.idempotency_keys == {}


async def test_execute_subscription_change_charges_upgrade_difference(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
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
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )

    result = await execute_subscription_change(
        RequestContext(request_id="req_2", user_id="user_1"),
        "sub_123",
        SubscriptionChangeCommand(
            confirmation_token=preview.confirmation_token,
            confirmed=True,
        ),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.billing_retries,
        test_dependencies.payment_stores.payment_customers,
        test_dependencies.payment_stores.idempotency_keys,
        test_dependencies.payment_provider,
        test_dependencies.clock,
        test_dependencies.billing_key_cipher,
        idempotency_key="subscription-change-key",
        operation_locks=test_dependencies.operation_locks,
        subscription_change_uow_factory=(
            test_dependencies.subscription_change_uow_factory
        ),
    )

    payment = next(iter(test_dependencies.billing_retries.payments.values()))
    invoice = next(iter(test_dependencies.billing_retries.invoices.values()))
    audit = next(
        iter(test_dependencies.payment_stores.operator_audits.operator_audits.values())
    )
    assert result.server_decision == "upgrade"
    assert result.plan_id == "plan_pro_monthly"
    assert result.payment is not None
    assert result.payment["paymentId"] == payment.id
    assert result.payment["invoiceId"] == invoice.id
    assert result.payment["status"] == "paid"
    assert result.notification == {
        "template": "subscription_plan_upgrade_receipt",
        "queued": True,
    }
    assert payment.status == "paid"
    assert payment.amount == 3500
    assert payment.payment_customer_id == payment_customer.id
    assert payment.billing_method_id == "bm_1"
    assert invoice.status == "paid"
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1
    assert (
        test_dependencies.payment_provider.last_billing_charge_billing_key
        == "billing_key_secret"
    )
    assert (
        test_dependencies.payment_provider.last_billing_charge_idempotency_key
        == preview.confirmation_token
    )
    assert test_dependencies.operation_locks.acquire_calls == ["subscription:sub_123"]
    assert test_dependencies.subscription_change_uow_factory.commit_count == 1
    assert audit.action == "subscription.plan_change"
    assert audit.next_state["target_plan_id"] == "plan_pro_monthly"
    assert audit.next_state["payment"] == result.payment


async def test_execute_subscription_change_marks_upgrade_payment_failed(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
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
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )
    command = SubscriptionChangeCommand(
        confirmation_token=preview.confirmation_token,
        confirmed=True,
    )
    test_dependencies.payment_provider.charge_billing_key_error = ProviderError(
        "card company rejected plan change",
        provider_code="REJECT_CARD_COMPANY",
    )

    with pytest.raises(PaymentRequiredResponseError) as exc_info:
        await execute_subscription_change(
            RequestContext(request_id="req_2", user_id="user_1"),
            "sub_123",
            command,
            test_dependencies.subscription_accounts,
            test_dependencies.catalog_repository,
            test_dependencies.subscription_change_tokens,
            test_dependencies.billing_retries,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            idempotency_key="subscription-change-key",
            operation_locks=test_dependencies.operation_locks,
            subscription_change_uow_factory=(
                test_dependencies.subscription_change_uow_factory
            ),
        )

    subscription = test_dependencies.subscription_accounts.subscriptions["sub_123"]
    payment = next(iter(test_dependencies.billing_retries.payments.values()))
    invoice = next(iter(test_dependencies.billing_retries.invoices.values()))
    idempotency_key = next(
        iter(test_dependencies.payment_stores.idempotency_keys.idempotency_keys.values())
    )
    audit = next(
        iter(test_dependencies.payment_stores.operator_audits.operator_audits.values())
    )
    body = exc_info.value.response_body
    assert subscription.plan_id == "plan_basic_monthly"
    assert payment.status == "failed"
    assert payment.failure == {
        "code": "PLAN_CHANGE_CHARGE_FAILED",
        "providerCode": "REJECT_CARD_COMPANY",
        "message": "card company rejected plan change",
        "retryable": True,
        "phase": "charge",
        "reason": "provider_rejected",
    }
    assert invoice.status == "issued"
    assert body["planId"] == "plan_basic_monthly"
    assert body["payment"] == {
        "invoiceId": invoice.id,
        "paymentId": payment.id,
        "status": "failed",
        "amount": 3500,
        "currency": "KRW",
        "receiptUrl": None,
        "failure": payment.failure,
    }
    assert idempotency_key.status == "failed"
    assert idempotency_key.response_status == 402
    assert audit.action == "subscription.plan_change"
    assert audit.result == "failed"
    assert audit.next_state["target_plan_id"] == "plan_pro_monthly"
    assert audit.next_state["payment"] == body["payment"]
    assert test_dependencies.subscription_change_uow_factory.commit_count == 1

    with pytest.raises(PaymentRequiredResponseError):
        await execute_subscription_change(
            RequestContext(request_id="req_3", user_id="user_1"),
            "sub_123",
            command,
            test_dependencies.subscription_accounts,
            test_dependencies.catalog_repository,
            test_dependencies.subscription_change_tokens,
            test_dependencies.billing_retries,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            idempotency_key="subscription-change-key",
            operation_locks=test_dependencies.operation_locks,
            subscription_change_uow_factory=(
                test_dependencies.subscription_change_uow_factory
            ),
        )
    assert test_dependencies.payment_provider.charge_billing_key_call_count == 1


async def test_execute_subscription_change_fails_upgrade_on_provider_mismatch(
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
        current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
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
    preview = await create_subscription_change_preview(
        RequestContext(request_id="req_1", user_id="user_1"),
        "sub_123",
        SubscriptionChangePreviewCommand(target_plan_id="plan_pro_monthly"),
        test_dependencies.subscription_accounts,
        test_dependencies.catalog_repository,
        test_dependencies.subscription_change_tokens,
        test_dependencies.clock,
    )
    test_dependencies.payment_provider.charge_billing_key_result = (
        BillingChargeProviderResult(
            payment_key="paykey_plan_change",
            order_id="ord_mismatch",
            amount=3500,
            approved_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/payment",
            method="카드",
            method_detail={"maskedCardNumber": "**** **** **** 1234"},
            response_summary={"provider": "tosspayments"},
        )
    )

    with pytest.raises(PaymentRequiredResponseError) as exc_info:
        await execute_subscription_change(
            RequestContext(request_id="req_2", user_id="user_1"),
            "sub_123",
            SubscriptionChangeCommand(
                confirmation_token=preview.confirmation_token,
                confirmed=True,
            ),
            test_dependencies.subscription_accounts,
            test_dependencies.catalog_repository,
            test_dependencies.subscription_change_tokens,
            test_dependencies.billing_retries,
            test_dependencies.payment_stores.payment_customers,
            test_dependencies.payment_stores.idempotency_keys,
            test_dependencies.payment_provider,
            test_dependencies.clock,
            test_dependencies.billing_key_cipher,
            idempotency_key="subscription-change-key",
            operation_locks=test_dependencies.operation_locks,
            subscription_change_uow_factory=(
                test_dependencies.subscription_change_uow_factory
            ),
        )

    subscription = test_dependencies.subscription_accounts.subscriptions["sub_123"]
    payment = next(iter(test_dependencies.billing_retries.payments.values()))
    invoice = next(iter(test_dependencies.billing_retries.invoices.values()))
    idempotency_key = next(
        iter(test_dependencies.payment_stores.idempotency_keys.idempotency_keys.values())
    )
    body = exc_info.value.response_body
    assert subscription.plan_id == "plan_basic_monthly"
    assert payment.status == "failed"
    assert payment.failure == {
        "code": "PLAN_CHANGE_CHARGE_FAILED",
        "providerCode": "PROVIDER_BILLING_CHARGE_FAILED",
        "message": "provider billing charge response does not match request",
        "retryable": True,
        "phase": "charge",
        "reason": "provider_error",
    }
    assert invoice.status == "issued"
    assert body["payment"] == {
        "invoiceId": invoice.id,
        "paymentId": payment.id,
        "status": "failed",
        "amount": 3500,
        "currency": "KRW",
        "receiptUrl": None,
        "failure": payment.failure,
    }
    assert idempotency_key.status == "failed"
    assert idempotency_key.response_status == 402
