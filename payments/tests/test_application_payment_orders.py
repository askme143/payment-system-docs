from __future__ import annotations

import pytest

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    BadRequestError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentConfirmRejectedError,
    ProviderError,
    ResourceNotFoundError,
)
from payments.application.payment_orders import (
    PaymentAuthFailureCommand,
    PaymentCancelCommand,
    PaymentConfirmCommand,
    PaymentOrderItem,
    PaymentOrderResult,
    cancel_payment,
    confirm_payment,
    create_payment_order,
    get_payment_detail,
    record_payment_auth_failure,
)
from payments.application.ports import (
    PaymentCancelProviderResult,
    PaymentConfirmProviderResult,
)


def items() -> list[PaymentOrderItem]:
    return [PaymentOrderItem(sku_id="sku_report_pack_100", quantity=2)]


async def create_confirmed_payment(
    test_dependencies,
    *,
    user_id: str = "user_1",
    request_id: str = "req_1",
    confirm_request_id: str = "req_2",
    payment_key: str = "paykey_123",
    confirm_key: str = "confirm-key",
) -> PaymentOrderResult:
    result = await create_payment_order(
        requester=RequestContext(request_id=request_id, user_id=user_id),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id=confirm_request_id, user_id=user_id),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key=payment_key,
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key=confirm_key,
    )
    return result


async def test_create_payment_order_requires_user(test_dependencies) -> None:
    with pytest.raises(AuthorizationError):
        await create_payment_order(
            requester=RequestContext(request_id="req_1"),
            items=items(),
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
        )


async def test_create_payment_order_creates_ready_payment(test_dependencies) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    assert result.checkout_id.startswith("chk_")
    assert result.payment_id.startswith("pay_")
    assert result.order_id.startswith("order_")
    assert result.attempt_no == 1
    assert result.order_name == "REPORT_PACK_100"
    assert result.amount == 50000
    assert result.currency == "KRW"
    assert result.customer_key.startswith("pcus_key_")
    assert result.success_url.startswith("https://example.com/success?")
    assert f"paymentId={result.payment_id}" in result.success_url
    assert result.fail_url.startswith("https://example.com/fail?")
    assert f"paymentId={result.payment_id}" in result.fail_url
    assert result.status == "ready"
    assert result.expires_at == test_dependencies.clock.utc_now().replace(minute=30)


async def test_create_payment_order_creates_payment_customer(test_dependencies) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    customers = test_dependencies.payment_stores.payment_customers.payment_customers
    customer = next(iter(customers.values()))
    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]

    assert customer.user_id == "user_1"
    assert customer.provider == "tosspayments"
    assert customer.status == "active"
    assert customer.customer_key == result.customer_key
    assert checkout.payment_customer_id == customer.id


async def test_create_payment_order_stores_sku_items(test_dependencies) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]

    assert checkout.items == [
        {
            "skuId": "sku_report_pack_100",
            "quantity": 2,
            "unitAmount": 25000,
            "amount": 50000,
        }
    ]


async def test_create_payment_order_rejects_inactive_sku(test_dependencies) -> None:
    test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ].status = "paused"

    with pytest.raises(ResourceNotFoundError):
        await create_payment_order(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            items=items(),
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
        )


async def test_create_payment_order_rejects_insufficient_limited_stock(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 1
    sku.reserved_stock = 0
    sku.sold_stock = 0

    with pytest.raises(InvalidStateTransitionError):
        await create_payment_order(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            items=items(),
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
            idempotency_key="insufficient-stock-key",
        )

    assert test_dependencies.payment_stores.checkouts.checkouts == {}
    assert test_dependencies.payment_stores.payments.payments == {}
    assert test_dependencies.payment_stores.idempotency_keys.idempotency_keys == {}


async def test_create_payment_order_reserves_limited_stock(test_dependencies) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 1
    sku.sold_stock = 0

    await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    assert sku.reserved_stock == 3


async def test_create_payment_order_rejects_per_order_purchase_limit(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.purchase_limit = {"perOrder": 1}

    with pytest.raises(InvalidStateTransitionError):
        await create_payment_order(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            items=items(),
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
        )

    assert test_dependencies.payment_stores.checkouts.checkouts == {}
    assert test_dependencies.payment_stores.payments.payments == {}


async def test_create_payment_order_rejects_per_user_purchase_limit(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.purchase_limit = {"perUser": 2}
    await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    with pytest.raises(InvalidStateTransitionError):
        await create_payment_order(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            items=[PaymentOrderItem(sku_id="sku_report_pack_100", quantity=1)],
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
        )

    assert len(test_dependencies.payment_stores.checkouts.checkouts) == 1
    assert len(test_dependencies.payment_stores.payments.payments) == 1


async def test_create_payment_order_replays_same_idempotency_key(
    test_dependencies,
) -> None:
    kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "items": items(),
        "success_url": "https://example.com/success",
        "fail_url": "https://example.com/fail",
        "one_time_payment_uow_factory": (
            test_dependencies.one_time_payment_uow_factory
        ),
        "clock": test_dependencies.clock,
        "idempotency_key": "same-key",
    }

    first = await create_payment_order(**kwargs)
    second = await create_payment_order(**kwargs)

    assert second == first


async def test_create_payment_order_retry_increments_attempt_number(
    test_dependencies,
) -> None:
    first = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=first.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=first.order_id,
            code="PAY_PROCESS_CANCELED",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    retry = await create_payment_order(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
        checkout_id=first.checkout_id,
    )

    assert retry.checkout_id == first.checkout_id
    assert retry.payment_id != first.payment_id
    assert retry.order_id != first.order_id
    assert retry.attempt_no == 2
    assert (
        test_dependencies.payment_stores.checkouts.checkouts[first.checkout_id].status
        == "ready"
    )


async def test_payment_detail_returns_attempt_number_for_requested_payment(
    test_dependencies,
) -> None:
    first = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=first.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=first.order_id,
            code="PAY_PROCESS_CANCELED",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    retry = await create_payment_order(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
        checkout_id=first.checkout_id,
    )

    first_detail = await get_payment_detail(
        requester=RequestContext(request_id="req_4", user_id="user_1"),
        payment_id=first.payment_id,
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    retry_detail = await get_payment_detail(
        requester=RequestContext(request_id="req_5", user_id="user_1"),
        payment_id=retry.payment_id,
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    assert first_detail.attempt_no == 1
    assert retry_detail.attempt_no == 2


async def test_confirm_payment_returns_retry_attempt_number(test_dependencies) -> None:
    first = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=first.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=first.order_id,
            code="PAY_PROCESS_CANCELED",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    retry = await create_payment_order(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
        checkout_id=first.checkout_id,
    )

    confirmed = await confirm_payment(
        requester=RequestContext(request_id="req_4", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=retry.payment_id,
            payment_key="paykey_retry",
            order_id=retry.order_id,
            amount=retry.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-retry-key",
    )

    assert confirmed.attempt_no == 2


async def test_create_payment_order_rejects_retry_for_ready_checkout_without_stock_leak(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 10
    sku.reserved_stock = 0
    sku.sold_stock = 0
    first = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    with pytest.raises(InvalidStateTransitionError):
        await create_payment_order(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            items=items(),
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
            checkout_id=first.checkout_id,
        )

    assert sku.reserved_stock == 2
    assert (
        test_dependencies.payment_stores.checkouts.checkouts[first.checkout_id].status
        == "ready"
    )


async def test_create_payment_order_rejects_changed_retry_items_without_stock_leak(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 10
    sku.reserved_stock = 0
    sku.sold_stock = 0
    first = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=first.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=first.order_id,
            code="PAY_PROCESS_CANCELED",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    with pytest.raises(InvalidStateTransitionError):
        await create_payment_order(
            requester=RequestContext(request_id="req_3", user_id="user_1"),
            items=[PaymentOrderItem(sku_id="sku_report_pack_100", quantity=1)],
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
            checkout_id=first.checkout_id,
        )

    assert sku.reserved_stock == 0
    assert (
        test_dependencies.payment_stores.checkouts.checkouts[first.checkout_id].status
        == "failed"
    )


async def test_create_payment_order_rejects_idempotency_conflict(
    test_dependencies,
) -> None:
    kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "items": items(),
        "success_url": "https://example.com/success",
        "fail_url": "https://example.com/fail",
        "one_time_payment_uow_factory": (
            test_dependencies.one_time_payment_uow_factory
        ),
        "clock": test_dependencies.clock,
        "idempotency_key": "same-key",
    }
    await create_payment_order(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        await create_payment_order(
            **{**kwargs, "items": [PaymentOrderItem("sku_report_pack_100", 3)]}
        )


async def test_get_payment_detail_enforces_ownership(test_dependencies) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    detail = await get_payment_detail(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    assert detail.payment_id == result.payment_id
    assert detail.checkout_id == result.checkout_id
    assert detail.order_id == result.order_id
    assert detail.attempt_no == 1
    assert detail.amount == result.amount
    assert detail.currency == "KRW"
    assert detail.order_name == "REPORT_PACK_100"
    assert detail.status == "ready"
    assert detail.failure is None
    assert detail.retry == {"available": False}
    with pytest.raises(ForbiddenError):
        await get_payment_detail(
            requester=RequestContext(request_id="req_3", user_id="user_2"),
            payment_id=result.payment_id,
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
        )


async def test_get_payment_detail_lazy_expires_ready_payment_and_releases_stock(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    payment.expires_at = test_dependencies.clock.utc_now()
    assert sku.reserved_stock == 2

    detail = await get_payment_detail(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    assert detail.status == "expired"
    assert detail.failure == {
        "phase": "before_confirm",
        "reason": "auth_result_not_reported",
        "retryable": True,
    }
    assert detail.retry == {
        "available": True,
        "action": "create_new_payment_attempt",
        "checkoutId": result.checkout_id,
    }
    assert sku.reserved_stock == 0
    assert payment.status == "expired"
    assert (
        test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id].status
        == "expired"
    )


async def test_record_payment_auth_failure_marks_ready_payment_failed(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    failure = await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=result.order_id,
            code="PAY_PROCESS_CANCELED",
            message="user canceled",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]
    assert failure.status == "failed"
    assert failure.failure["reason"] == "user_canceled"
    assert failure.retry == {
        "available": True,
        "action": "create_new_payment_attempt",
        "checkoutId": result.checkout_id,
    }
    assert payment.status == "failed"
    assert payment.failure == failure.failure
    assert checkout.status == "failed"


async def test_record_payment_auth_failure_preserves_expired_unreported_reason(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    payment.expires_at = test_dependencies.clock.utc_now()

    failure = await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=result.order_id,
            code="PAY_PROCESS_CANCELED",
            message="user canceled after expiration",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    assert failure.status == "expired"
    assert failure.failure == {
        "phase": "before_confirm",
        "reason": "auth_result_not_reported",
        "retryable": True,
    }
    assert payment.failure == failure.failure
    assert sku.reserved_stock == 0


async def test_record_payment_auth_failure_replays_same_idempotency_key(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    first = await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=result.order_id,
            code="PAY_PROCESS_CANCELED",
            message="user canceled",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
        idempotency_key="auth-result-key",
    )
    replayed = await record_payment_auth_failure(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=result.order_id,
            code="PAY_PROCESS_CANCELED",
            message="user canceled",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
        idempotency_key="auth-result-key",
    )

    assert replayed == first


async def test_record_payment_auth_failure_rejects_idempotency_conflict(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=result.order_id,
            code="PAY_PROCESS_CANCELED",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
        idempotency_key="auth-result-key",
    )

    with pytest.raises(IdempotencyConflictError):
        await record_payment_auth_failure(
            requester=RequestContext(request_id="req_3", user_id="user_1"),
            payment_id=result.payment_id,
            command=PaymentAuthFailureCommand(
                order_id=result.order_id,
                code="PROVIDER_AUTH_FAILED",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
            idempotency_key="auth-result-key",
        )


async def test_record_payment_auth_failure_rejects_order_mismatch_as_bad_request(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    with pytest.raises(BadRequestError):
        await record_payment_auth_failure(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            payment_id=result.payment_id,
            command=PaymentAuthFailureCommand(
                order_id="other_order",
                code="PAY_PROCESS_CANCELED",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
        )


async def test_record_payment_auth_failure_rejects_other_user_with_forbidden(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    with pytest.raises(ForbiddenError):
        await record_payment_auth_failure(
            requester=RequestContext(request_id="req_2", user_id="user_2"),
            payment_id=result.payment_id,
            command=PaymentAuthFailureCommand(
                order_id=result.order_id,
                code="PAY_PROCESS_CANCELED",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            clock=test_dependencies.clock,
        )


async def test_record_payment_auth_failure_releases_limited_reserved_stock(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    assert sku.reserved_stock == 2

    await record_payment_auth_failure(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentAuthFailureCommand(
            order_id=result.order_id,
            code="PAY_PROCESS_CANCELED",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )

    assert sku.reserved_stock == 0


async def test_confirm_payment_marks_payment_paid_and_captures_stock(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    provider = test_dependencies.payment_provider

    confirmed = await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
        operation_locks=test_dependencies.operation_locks,
    )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]
    invoices = test_dependencies.payment_stores.invoices.invoices
    assert confirmed.payment_id == result.payment_id
    assert confirmed.status == "paid"
    assert confirmed.payment_key == "paykey_123"
    assert payment.status == "paid"
    assert checkout.status == "paid"
    assert len(invoices) == 1
    invoice = next(iter(invoices.values()))
    assert invoice.user_id == "user_1"
    assert invoice.payment_id == result.payment_id
    assert invoice.status == "paid"
    assert invoice.receipt_url == payment.receipt_url
    assert sku.reserved_stock == 0
    assert sku.sold_stock == 2
    assert provider.confirm_payment_call_count == 1
    assert provider.last_confirm_payment_idempotency_key == "confirm-key"
    assert test_dependencies.operation_locks.acquire_calls == [
        f"payment-confirm:{result.payment_id}",
        f"checkout-confirm:{result.checkout_id}",
    ]
    assert test_dependencies.operation_locks.release_calls == [
        f"checkout-confirm:{result.checkout_id}",
        f"payment-confirm:{result.payment_id}",
    ]


async def test_confirm_payment_rejects_when_payment_confirm_is_locked(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    lock_key = f"payment-confirm:{result.payment_id}"
    operation_lock = await test_dependencies.operation_locks.acquire_operation_lock(
        lock_key=lock_key,
        owner_token="other-owner",
        fencing_counter_key="payment-confirm",
        locked_until_at=test_dependencies.clock.utc_now(),
        acquired_at=test_dependencies.clock.utc_now(),
    )
    assert operation_lock is not None
    operation_lock.locked_until_at = operation_lock.locked_until_at.replace(
        minute=operation_lock.locked_until_at.minute + 5
    )

    with pytest.raises(InvalidStateTransitionError):
        await confirm_payment(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
            operation_locks=test_dependencies.operation_locks,
        )

    assert test_dependencies.payment_provider.confirm_payment_call_count == 0
    assert test_dependencies.operation_locks.release_calls == []


async def test_confirm_payment_rejects_when_checkout_confirm_is_locked(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    checkout_lock_key = f"checkout-confirm:{result.checkout_id}"
    operation_lock = await test_dependencies.operation_locks.acquire_operation_lock(
        lock_key=checkout_lock_key,
        owner_token="other-owner",
        fencing_counter_key="checkout-confirm",
        locked_until_at=test_dependencies.clock.utc_now(),
        acquired_at=test_dependencies.clock.utc_now(),
    )
    assert operation_lock is not None
    operation_lock.locked_until_at = operation_lock.locked_until_at.replace(
        minute=operation_lock.locked_until_at.minute + 5
    )

    with pytest.raises(InvalidStateTransitionError):
        await confirm_payment(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
            operation_locks=test_dependencies.operation_locks,
        )

    assert test_dependencies.payment_provider.confirm_payment_call_count == 0
    assert test_dependencies.operation_locks.acquire_calls == [
        checkout_lock_key,
        f"payment-confirm:{result.payment_id}",
        checkout_lock_key,
    ]
    assert test_dependencies.operation_locks.release_calls == [
        f"payment-confirm:{result.payment_id}"
    ]


async def test_confirm_payment_rejects_paid_checkout_before_provider_call(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    checkout = test_dependencies.payment_stores.checkouts.checkouts[
        result.checkout_id
    ]
    checkout.status = "paid"

    with pytest.raises(InvalidStateTransitionError):
        await confirm_payment(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
        )

    assert test_dependencies.payment_provider.confirm_payment_call_count == 0


async def test_confirm_payment_expires_attempt_and_allows_checkout_retry(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    payment.expires_at = test_dependencies.clock.utc_now()

    with pytest.raises(InvalidStateTransitionError):
        await confirm_payment(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
        )

    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]
    assert payment.status == "expired"
    assert payment.failure == {
        "phase": "before_confirm",
        "reason": "auth_result_not_reported",
        "retryable": True,
    }
    assert checkout.status == "expired"
    assert sku.reserved_stock == 0
    assert test_dependencies.payment_provider.confirm_payment_call_count == 0

    retry = await create_payment_order(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
        checkout_id=result.checkout_id,
    )

    assert retry.checkout_id == result.checkout_id
    assert retry.payment_id != result.payment_id
    assert retry.attempt_no == 2
    assert sku.reserved_stock == 2


async def test_confirm_payment_replays_same_idempotency_key_without_provider_call(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    provider = test_dependencies.payment_provider

    first = await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )
    second = await confirm_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )

    assert second == first
    assert provider.confirm_payment_call_count == 1


async def test_confirm_payment_replays_same_payment_success_without_provider_call(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    provider = test_dependencies.payment_provider
    command = PaymentConfirmCommand(
        payment_id=result.payment_id,
        payment_key="paykey_123",
        order_id=result.order_id,
        amount=result.amount,
    )

    first = await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=command,
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key-1",
    )
    second = await confirm_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        command=command,
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key-2",
    )

    assert second == first
    assert provider.confirm_payment_call_count == 1


async def test_confirm_payment_rejects_same_idempotency_key_with_different_payload(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )

    with pytest.raises(IdempotencyConflictError):
        await confirm_payment(
            requester=RequestContext(request_id="req_3", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="other_paykey",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
        )


async def test_confirm_payment_rejects_amount_mismatch(test_dependencies) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    assert sku.reserved_stock == 2

    with pytest.raises(BadRequestError):
        await confirm_payment(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount + 1,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
        )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]
    assert payment.status == "failed"
    assert payment.failure == {
        "code": "PAYMENT_CONFIRM_VALIDATION_FAILED",
        "providerCode": "PAYMENT_CONFIRM_VALIDATION_FAILED",
        "message": "amount does not match payment",
        "retryable": True,
        "phase": "confirm",
        "reason": "validation_failed",
    }
    assert checkout.status == "failed"
    assert sku.reserved_stock == 0
    assert test_dependencies.payment_provider.confirm_payment_call_count == 0


async def test_confirm_payment_marks_failed_when_provider_rejects(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    test_dependencies.payment_provider.confirm_payment_error = ProviderError(
        "card company rejected payment",
        provider_code="REJECT_CARD_COMPANY",
    )

    with pytest.raises(PaymentConfirmRejectedError) as exc_info:
        await confirm_payment(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
        )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]
    assert payment.status == "failed"
    assert checkout.status == "failed"
    assert payment.failure == {
        "code": "PAYMENT_CONFIRM_FAILED",
        "providerCode": "REJECT_CARD_COMPANY",
        "message": "card company rejected payment",
        "retryable": True,
        "phase": "confirm",
        "reason": "provider_rejected",
    }
    assert exc_info.value.response_body["status"] == "failed"
    assert exc_info.value.response_body["retry"] == {
        "available": True,
        "action": "create_new_payment_attempt",
        "checkoutId": result.checkout_id,
    }
    assert sku.reserved_stock == 0
    assert sku.sold_stock == 0
    test_dependencies.payment_provider.confirm_payment_error = None
    with pytest.raises(PaymentConfirmRejectedError) as replay_info:
        await confirm_payment(
            requester=RequestContext(request_id="req_3", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
        )
    assert replay_info.value.response_body == exc_info.value.response_body
    assert test_dependencies.payment_provider.confirm_payment_call_count == 1


async def test_confirm_payment_marks_failed_when_provider_result_mismatches(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    test_dependencies.payment_provider.confirm_payment_result = (
        PaymentConfirmProviderResult(
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount + 1,
            approved_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/payment",
            method="카드",
            method_detail={"maskedCardNumber": "**** **** **** 1234"},
            response_summary={"provider": "tosspayments"},
        )
    )

    with pytest.raises(PaymentConfirmRejectedError) as exc_info:
        await confirm_payment(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            command=PaymentConfirmCommand(
                payment_id=result.payment_id,
                payment_key="paykey_123",
                order_id=result.order_id,
                amount=result.amount,
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="confirm-key",
        )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    checkout = test_dependencies.payment_stores.checkouts.checkouts[result.checkout_id]
    assert payment.status == "failed"
    assert checkout.status == "failed"
    assert payment.failure == {
        "code": "PAYMENT_CONFIRM_FAILED",
        "providerCode": "PROVIDER_CONFIRM_FAILED",
        "message": "provider response does not match",
        "retryable": True,
        "phase": "confirm",
        "reason": "provider_error",
    }
    assert exc_info.value.response_body["failure"] == payment.failure
    assert sku.reserved_stock == 0
    assert sku.sold_stock == 0


async def test_cancel_payment_records_full_cancel(test_dependencies) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )

    canceled = await cancel_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="customer_request",
            reason_message="refund requested",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key",
    )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    assert canceled.status == "canceled"
    assert canceled.canceled_amount == result.amount
    assert canceled.cancelable_amount == 0
    assert isinstance(canceled.latest_cancel["cancelId"], str)
    assert canceled.latest_cancel["cancelId"].startswith("pcancel_")
    assert canceled.latest_cancel["providerCancelId"] == "cnl_123"
    assert canceled.latest_cancel["cancelReason"] == "customer_request"
    assert test_dependencies.payment_provider.last_cancel_payment_idempotency_key == (
        "cancel-key"
    )
    assert payment.status == "canceled"
    assert payment.cancelable_amount == 0
    assert sku.reserved_stock == 0
    assert sku.sold_stock == 0
    assert len(payment.cancel_history or []) == 1
    assert isinstance(payment.cancel_history[0]["cancelId"], str)
    assert payment.cancel_history[0]["cancelId"].startswith("pcancel_")
    assert payment.cancel_history[0]["providerCancelId"] == "cnl_123"
    cancel_requests = (
        test_dependencies.payment_stores.payment_cancel_requests.payment_cancel_requests
    )
    cancel_request = next(iter(cancel_requests.values()))
    assert cancel_request.payment_id == result.payment_id
    assert cancel_request.status == "succeeded"
    assert cancel_request.provider_cancel_id == "cnl_123"
    assert cancel_request.operator_audit_id is not None
    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        cancel_request.operator_audit_id
    ]
    assert audit.operator_id == "user_1"
    assert audit.action == "payment.cancel"
    assert audit.target_type == "payment"
    assert audit.target_id == result.payment_id
    assert audit.previous_state["status"] == "paid"
    assert audit.next_state["status"] == "canceled"
    assert audit.next_state["requested_by"] == "user"
    assert audit.next_state["notification"] == {
        "template": "payment_cancel_completed",
        "queued": True,
        "payload": {"cancelAmount": result.amount},
    }
    assert audit.reason_code == "customer_request"
    assert audit.idempotency_scope == "payments-cancel"


async def test_cancel_payment_forwards_refund_bank_account(
    test_dependencies,
) -> None:
    result = await create_confirmed_payment(test_dependencies)
    refund_bank_account: dict[str, object] = {
        "bank": "088",
        "accountNumber": "1234567890",
        "holderName": "홍길동",
    }

    await cancel_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="customer_request",
            refund_bank_account=refund_bank_account,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key",
    )

    assert (
        test_dependencies.payment_provider.last_cancel_payment_refund_bank_account
        == refund_bank_account
    )


async def test_cancel_payment_rejects_amount_over_cancelable(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )

    with pytest.raises(BadRequestError):
        await cancel_payment(
            requester=RequestContext(request_id="req_3", user_id="user_1"),
            payment_id=result.payment_id,
            command=PaymentCancelCommand(
                cancel_amount=result.amount + 1,
                cancel_reason="customer_request",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="cancel-key",
        )


async def test_cancel_payment_rejects_other_user_with_forbidden(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )

    with pytest.raises(ForbiddenError):
        await cancel_payment(
            requester=RequestContext(request_id="req_3", user_id="user_2"),
            payment_id=result.payment_id,
            command=PaymentCancelCommand(
                cancel_amount=None,
                cancel_reason="customer_request",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="cancel-key",
        )


async def test_cancel_payment_replays_same_idempotency_key_without_provider_call(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )

    first = await cancel_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key",
    )
    replayed = await cancel_payment(
        requester=RequestContext(request_id="req_4", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key",
    )

    assert replayed == first
    assert test_dependencies.payment_provider.cancel_payment_call_count == 1


async def test_cancel_payment_scopes_idempotency_key_by_payment_id(
    test_dependencies,
) -> None:
    first = await create_confirmed_payment(
        test_dependencies,
        request_id="req_1",
        confirm_request_id="req_2",
        payment_key="paykey_1",
        confirm_key="confirm-key-1",
    )
    second = await create_confirmed_payment(
        test_dependencies,
        request_id="req_3",
        confirm_request_id="req_4",
        payment_key="paykey_2",
        confirm_key="confirm-key-2",
    )

    first_cancel = await cancel_payment(
        requester=RequestContext(request_id="req_5", user_id="user_1"),
        payment_id=first.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="shared-cancel-key",
        operation_locks=test_dependencies.operation_locks,
    )
    second_cancel = await cancel_payment(
        requester=RequestContext(request_id="req_6", user_id="user_1"),
        payment_id=second.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="shared-cancel-key",
        operation_locks=test_dependencies.operation_locks,
    )

    assert first_cancel.payment_id == first.payment_id
    assert second_cancel.payment_id == second.payment_id
    assert first_cancel.status == "canceled"
    assert second_cancel.status == "canceled"
    assert test_dependencies.payment_provider.cancel_payment_call_count == 2


async def test_cancel_payment_rejects_when_payment_cancel_is_locked(
    test_dependencies,
) -> None:
    result = await create_confirmed_payment(test_dependencies)
    lock_key = f"payment-cancel:{result.payment_id}"
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
        await cancel_payment(
            requester=RequestContext(request_id="req_3", user_id="user_1"),
            payment_id=result.payment_id,
            command=PaymentCancelCommand(
                cancel_amount=None,
                cancel_reason="customer_request",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="cancel-key",
            operation_locks=test_dependencies.operation_locks,
        )

    assert test_dependencies.payment_provider.cancel_payment_call_count == 0
    assert test_dependencies.operation_locks.release_calls == []


async def test_cancel_payment_accepts_provider_cumulative_partial_cancel_amounts(
    test_dependencies,
) -> None:
    sku = test_dependencies.payment_stores.one_time_skus.one_time_skus[
        "sku_report_pack_100"
    ]
    sku.stock_policy = "limited"
    sku.total_stock = 5
    sku.reserved_stock = 0
    sku.sold_stock = 0
    result = await create_confirmed_payment(test_dependencies)
    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_1",
            cancel_amount=10_000,
            canceled_amount=10_000,
            cancelable_amount=40_000,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/cancel-1",
        )
    )

    first_cancel = await cancel_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=10_000,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key-1",
        operation_locks=test_dependencies.operation_locks,
    )

    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_2",
            cancel_amount=5_000,
            canceled_amount=15_000,
            cancelable_amount=35_000,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/cancel-2",
        )
    )
    second_cancel = await cancel_payment(
        requester=RequestContext(request_id="req_4", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=5_000,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key-2",
        operation_locks=test_dependencies.operation_locks,
    )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
    assert first_cancel.status == "partial_canceled"
    assert first_cancel.canceled_amount == 10_000
    assert first_cancel.cancelable_amount == 40_000
    assert second_cancel.status == "partial_canceled"
    assert second_cancel.canceled_amount == 15_000
    assert second_cancel.cancelable_amount == 35_000
    assert payment.cancelable_amount == 35_000
    assert sku.sold_stock == 2
    assert len(payment.cancel_history or []) == 2


async def test_cancel_payment_rejects_duplicate_provider_cancel_id(
    test_dependencies,
) -> None:
    result = await create_confirmed_payment(test_dependencies)
    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_duplicate",
            cancel_amount=10_000,
            canceled_amount=10_000,
            cancelable_amount=40_000,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/cancel-1",
        )
    )
    await cancel_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=10_000,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key-1",
        operation_locks=test_dependencies.operation_locks,
    )

    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_duplicate",
            cancel_amount=5_000,
            canceled_amount=15_000,
            cancelable_amount=35_000,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url="https://dashboard.tosspayments.com/receipt/cancel-2",
        )
    )
    with pytest.raises(ProviderError):
        await cancel_payment(
            requester=RequestContext(request_id="req_4", user_id="user_1"),
            payment_id=result.payment_id,
            command=PaymentCancelCommand(
                cancel_amount=5_000,
                cancel_reason="customer_request",
            ),
            one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="cancel-key-2",
            operation_locks=test_dependencies.operation_locks,
        )

    payment = test_dependencies.payment_stores.payments.payments[result.payment_id]
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


async def test_cancel_payment_rejects_same_idempotency_key_with_different_payload(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )
    await cancel_payment(
        requester=RequestContext(request_id="req_3", user_id="user_1"),
        payment_id=result.payment_id,
        command=PaymentCancelCommand(
            cancel_amount=None,
            cancel_reason="customer_request",
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="cancel-key",
    )

    with pytest.raises(IdempotencyConflictError):
        await cancel_payment(
            requester=RequestContext(request_id="req_4", user_id="user_1"),
            payment_id=result.payment_id,
            command=PaymentCancelCommand(
                cancel_amount=1000,
                cancel_reason="customer_request",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="cancel-key",
        )


async def test_cancel_payment_marks_request_failed_for_provider_mismatch(
    test_dependencies,
) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        clock=test_dependencies.clock,
    )
    await confirm_payment(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        command=PaymentConfirmCommand(
            payment_id=result.payment_id,
            payment_key="paykey_123",
            order_id=result.order_id,
            amount=result.amount,
        ),
        one_time_payment_uow_factory=test_dependencies.one_time_payment_uow_factory,
        provider=test_dependencies.payment_provider,
        clock=test_dependencies.clock,
        idempotency_key="confirm-key",
    )
    test_dependencies.payment_provider.cancel_payment_result = (
        PaymentCancelProviderResult(
            cancel_id="cnl_bad",
            cancel_amount=result.amount - 1,
            canceled_amount=result.amount - 1,
            cancelable_amount=1,
            canceled_at=test_dependencies.clock.utc_now(),
            receipt_url=None,
        )
    )

    with pytest.raises(ProviderError):
        await cancel_payment(
            requester=RequestContext(request_id="req_3", user_id="user_1"),
            payment_id=result.payment_id,
            command=PaymentCancelCommand(
                cancel_amount=None,
                cancel_reason="customer_request",
            ),
            one_time_payment_uow_factory=(
                test_dependencies.one_time_payment_uow_factory
            ),
            provider=test_dependencies.payment_provider,
            clock=test_dependencies.clock,
            idempotency_key="cancel-key",
        )

    cancel_request = next(
        iter(
            test_dependencies.payment_stores.payment_cancel_requests.payment_cancel_requests.values()
        )
    )
    assert cancel_request.status == "failed"
    assert cancel_request.failure == {
        "message": "provider response does not match",
        "retryable": True,
    }
    audit = test_dependencies.payment_stores.operator_audits.operator_audits[
        cancel_request.operator_audit_id
    ]
    assert audit.operator_id == "user_1"
    assert audit.action == "payment.cancel"
    assert audit.target_id == result.payment_id
    assert audit.result == "failed"
    assert audit.reason_code == "customer_request"
    assert audit.previous_state["status"] == "paid"
    assert audit.next_state["failure"] == {
        "message": "provider response does not match",
        "retryable": True,
    }
