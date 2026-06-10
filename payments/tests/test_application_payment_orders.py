from __future__ import annotations

import pytest

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    IdempotencyConflictError,
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
            payment_repository=test_dependencies.payment_repository,
            clock=test_dependencies.clock,
        )


async def test_create_payment_order_creates_ready_payment(test_dependencies) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        payment_repository=test_dependencies.payment_repository,
        clock=test_dependencies.clock,
    )

    assert result.checkout_id.startswith("chk_")
    assert result.payment_id.startswith("pay_")
    assert result.order_id.startswith("order_")
    assert result.amount == 2000
    assert result.status == "ready"


async def test_create_payment_order_stores_sku_items(test_dependencies) -> None:
    result = await create_payment_order(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        items=items(),
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        payment_repository=test_dependencies.payment_repository,
        clock=test_dependencies.clock,
    )

    checkout = test_dependencies.payment_repository.checkouts[result.checkout_id]

    assert checkout.items == [{"skuId": "sku_report_pack_100", "quantity": 2}]


async def test_create_payment_order_replays_same_idempotency_key(
    test_dependencies,
) -> None:
    kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "items": items(),
        "success_url": "https://example.com/success",
        "fail_url": "https://example.com/fail",
        "payment_repository": test_dependencies.payment_repository,
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
        "payment_repository": test_dependencies.payment_repository,
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
        payment_repository=test_dependencies.payment_repository,
        clock=test_dependencies.clock,
    )

    detail = await get_payment_detail(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        payment_id=result.payment_id,
        payment_repository=test_dependencies.payment_repository,
    )

    assert detail.id == result.payment_id
    with pytest.raises(ResourceNotFoundError):
        await get_payment_detail(
            requester=RequestContext(request_id="req_3", user_id="user_2"),
            payment_id=result.payment_id,
            payment_repository=test_dependencies.payment_repository,
        )
