from __future__ import annotations

import pytest

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.payment_orders import (
    PaymentOrderItem,
    create_payment_order,
    get_payment_detail,
)


def items() -> list[PaymentOrderItem]:
    return [PaymentOrderItem(sku_id="sku_report_pack_100", quantity=2)]


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
    assert result.amount == 50000
    assert result.status == "ready"


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
        )


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
        payments=test_dependencies.payment_attempts,
    )

    assert detail.id == result.payment_id
    with pytest.raises(ResourceNotFoundError):
        await get_payment_detail(
            requester=RequestContext(request_id="req_3", user_id="user_2"),
            payment_id=result.payment_id,
            payments=test_dependencies.payment_attempts,
        )
