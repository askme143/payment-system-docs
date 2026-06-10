from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header

from payments.application.context import RequestContext
from payments.application.payment_orders import create_payment_order, get_payment_detail
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.payments import (
    CreatePaymentOrderRequest,
    PaymentDetailResponse,
    PaymentOrderResponse,
    payment_detail_response,
    payment_order_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["payments"])

    require_user_context = request_context_dependency(
        dependencies.internal_service_token, True
    )

    @router.post("/payments/orders", response_model=PaymentOrderResponse)
    async def create_order(
        body: CreatePaymentOrderRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> PaymentOrderResponse:
        result = await create_payment_order(
            requester=ctx,
            items=body.to_application_items(),
            success_url=str(body.success_url),
            fail_url=str(body.fail_url),
            one_time_payment_uow_factory=dependencies.one_time_payment_uow_factory,
            clock=dependencies.clock,
            idempotency_key=idempotency_key,
            checkout_id=body.checkout_id,
        )
        return payment_order_response(result)

    @router.get("/payments/{paymentId}", response_model=PaymentDetailResponse)
    async def get_payment(
        paymentId: str,
        ctx: RequestContext = Depends(require_user_context),
    ) -> PaymentDetailResponse:
        detail = await get_payment_detail(
            requester=ctx,
            payment_id=paymentId,
            payments=dependencies.payment_attempts,
        )
        return payment_detail_response(detail)

    return router
