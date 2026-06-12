from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, status

from payments.application.context import RequestContext
from payments.application.errors import BadRequestError
from payments.application.payment_orders import (
    PaymentAuthFailureCommand,
    PaymentCancelCommand,
    PaymentConfirmCommand,
    PaymentOrderItem,
    cancel_payment,
    confirm_payment,
    create_payment_order,
    get_payment_detail,
    record_payment_auth_failure,
)
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.payments import (
    CreatePaymentOrderRequest,
    PaymentAuthFailureResponse,
    PaymentAuthResultRequest,
    PaymentCancelRequest,
    PaymentCancelResponse,
    PaymentConfirmRequest,
    PaymentConfirmResponse,
    PaymentDetailResponse,
    PaymentOrderResponse,
    payment_auth_failure_response,
    payment_cancel_response,
    payment_confirm_response,
    payment_detail_response,
    payment_order_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["payments"])

    require_user_context = request_context_dependency(
        dependencies.internal_service_token, True
    )

    @router.post(
        "/payments/orders",
        response_model=PaymentOrderResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_order(
        body: CreatePaymentOrderRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> PaymentOrderResponse:
        result = await create_payment_order(
            requester=ctx,
            items=_payment_order_items(body.items),
            success_url=_http_url_text(body.success_url, "successUrl"),
            fail_url=_http_url_text(body.fail_url, "failUrl"),
            one_time_payment_uow_factory=dependencies.one_time_payment_uow_factory,
            clock=dependencies.clock,
            client_key=dependencies.toss_client_key,
            idempotency_key=idempotency_key,
            checkout_id=_optional_text(body.checkout_id, "checkoutId"),
        )
        return payment_order_response(result)

    @router.post("/payments/confirm", response_model=PaymentConfirmResponse)
    async def confirm(
        body: PaymentConfirmRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> PaymentConfirmResponse:
        result = await confirm_payment(
            requester=ctx,
            command=PaymentConfirmCommand(
                payment_id=_required_text(body.payment_id, "paymentId"),
                payment_key=_required_text(body.payment_key, "paymentKey"),
                order_id=_required_text(body.order_id, "orderId"),
                amount=_required_int(body.amount, "amount"),
            ),
            one_time_payment_uow_factory=dependencies.one_time_payment_uow_factory,
            provider=dependencies.payment_provider,
            clock=dependencies.clock,
            idempotency_key=_required_header(idempotency_key, "Idempotency-Key"),
            operation_locks=dependencies.operation_locks,
        )
        return payment_confirm_response(result)

    @router.post(
        "/payments/{paymentId}/auth-result",
        response_model=PaymentAuthFailureResponse,
    )
    async def record_auth_result(
        paymentId: str,
        body: PaymentAuthResultRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> PaymentAuthFailureResponse:
        result = await record_payment_auth_failure(
            requester=ctx,
            payment_id=paymentId,
            command=PaymentAuthFailureCommand(
                order_id=_required_text(body.order_id, "orderId"),
                code=_required_text(body.code, "code"),
                message=_optional_text(body.message, "message"),
            ),
            one_time_payment_uow_factory=dependencies.one_time_payment_uow_factory,
            clock=dependencies.clock,
            idempotency_key=idempotency_key,
        )
        return payment_auth_failure_response(result)

    @router.post(
        "/payments/{paymentId}/cancel",
        response_model=PaymentCancelResponse,
    )
    async def cancel(
        paymentId: str,
        body: PaymentCancelRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ctx: RequestContext = Depends(require_user_context),
    ) -> PaymentCancelResponse:
        result = await cancel_payment(
            requester=ctx,
            payment_id=paymentId,
            command=PaymentCancelCommand(
                cancel_amount=_optional_positive_int(
                    body.cancel_amount,
                    "cancelAmount",
                ),
                cancel_reason=_required_text(body.cancel_reason, "cancelReason"),
                reason_message=_optional_text(body.reason_message, "reasonMessage"),
                refund_bank_account=_optional_dict(
                    body.refund_bank_account,
                    "refundBankAccount",
                ),
            ),
            one_time_payment_uow_factory=dependencies.one_time_payment_uow_factory,
            provider=dependencies.payment_provider,
            clock=dependencies.clock,
            idempotency_key=_required_header(idempotency_key, "Idempotency-Key"),
            operation_locks=dependencies.operation_locks,
        )
        return payment_cancel_response(result)

    @router.get("/payments/{paymentId}", response_model=PaymentDetailResponse)
    async def get_payment(
        paymentId: str,
        ctx: RequestContext = Depends(require_user_context),
    ) -> PaymentDetailResponse:
        detail = await get_payment_detail(
            requester=ctx,
            payment_id=paymentId,
            one_time_payment_uow_factory=dependencies.one_time_payment_uow_factory,
            clock=dependencies.clock,
        )
        return payment_detail_response(detail)

    return router


def _required_header(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise BadRequestError(f"{field_name} header is required")
    return value


def _required_text(value: object | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value


def _optional_text(value: object | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is invalid")
    return value


def _required_int(value: object | None, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequestError(f"{field_name} is invalid")
    return value


def _optional_positive_int(value: object | None, field_name: str) -> int | None:
    if value is None:
        return None
    parsed = _required_int(value, field_name)
    if parsed < 1:
        raise BadRequestError(f"{field_name} is invalid")
    return parsed


def _required_positive_int(value: object | None, field_name: str) -> int:
    parsed = _required_int(value, field_name)
    if parsed < 1:
        raise BadRequestError(f"{field_name} is invalid")
    return parsed


def _optional_dict(
    value: object | None,
    field_name: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BadRequestError(f"{field_name} is invalid")
    return value


def _payment_order_items(value: object | None) -> list[PaymentOrderItem]:
    if not isinstance(value, list) or not value:
        raise BadRequestError("items are required")
    items: list[PaymentOrderItem] = []
    for item in value:
        if not isinstance(item, dict):
            raise BadRequestError("items are invalid")
        items.append(
            PaymentOrderItem(
                sku_id=_required_text(item.get("skuId"), "skuId"),
                quantity=_required_positive_int(item.get("quantity"), "quantity"),
            )
        )
    return items


def _http_url_text(value: object | None, field_name: str) -> str:
    text = _required_text(value, field_name)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BadRequestError(f"{field_name} is invalid")
    return text
