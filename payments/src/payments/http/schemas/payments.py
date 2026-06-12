from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from payments.application.payment_orders import (
    PaymentAuthFailureResult,
    PaymentCancelResult,
    PaymentConfirmResult,
    PaymentDetail,
    PaymentOrderResult,
)

_PAYMENT_METHOD_DETAIL_SCHEMA = {
    "properties": {
        "type": {"type": "string"},
        "company": {"type": "string"},
        "maskedNumber": {"type": "string"},
    },
    "additionalProperties": True,
}
_PAYMENT_RETRY_SCHEMA = {
    "properties": {
        "available": {"type": "boolean"},
        "action": {"type": "string"},
        "checkoutId": {"type": "string"},
    },
    "required": ["available"],
    "additionalProperties": False,
}
_PAYMENT_LATEST_CANCEL_SCHEMA = {
    "properties": {
        "cancelId": {"type": "string"},
        "cancelAmount": {"type": "integer"},
        "cancelReason": {"type": "string"},
        "canceledAt": {"type": "string", "format": "date-time"},
        "receiptUrl": {"type": "string"},
    },
    "required": [
        "cancelId",
        "cancelAmount",
        "cancelReason",
        "canceledAt",
        "receiptUrl",
    ],
    "additionalProperties": True,
}


class CreatePaymentOrderRequest(BaseModel):
    items: object | None = None
    success_url: object | None = Field(default=None, alias="successUrl")
    fail_url: object | None = Field(default=None, alias="failUrl")
    checkout_id: object | None = Field(default=None, alias="checkoutId")


class PaymentOrderResponse(BaseModel):
    checkout_id: str = Field(alias="checkoutId")
    payment_id: str = Field(alias="paymentId")
    order_id: str = Field(alias="orderId")
    attempt_no: int = Field(alias="attemptNo")
    order_name: str = Field(alias="orderName")
    amount: int
    currency: str
    customer_key: str = Field(alias="customerKey")
    client_key: str = Field(alias="clientKey")
    success_url: str = Field(alias="successUrl")
    fail_url: str = Field(alias="failUrl")
    status: str
    expires_at: datetime = Field(alias="expiresAt")


class PaymentDetailResponse(BaseModel):
    checkout_id: str = Field(alias="checkoutId")
    payment_id: str = Field(alias="paymentId")
    order_id: str = Field(alias="orderId")
    attempt_no: int = Field(alias="attemptNo")
    status: str
    amount: int
    currency: str
    order_name: str = Field(alias="orderName")
    approved_at: datetime | None = Field(alias="approvedAt")
    receipt_url: str | None = Field(alias="receiptUrl")
    method: str | None
    method_detail: dict[str, object] | None = Field(
        alias="methodDetail",
        json_schema_extra=_PAYMENT_METHOD_DETAIL_SCHEMA,
    )
    failure: dict[str, object] | None
    retry: dict[str, object] = Field(json_schema_extra=_PAYMENT_RETRY_SCHEMA)


class PaymentAuthResultRequest(BaseModel):
    order_id: object | None = Field(default=None, alias="orderId")
    code: object | None = None
    message: object | None = None


class PaymentAuthFailureResponse(BaseModel):
    checkout_id: str = Field(alias="checkoutId")
    payment_id: str = Field(alias="paymentId")
    order_id: str = Field(alias="orderId")
    status: str
    failure: dict[str, object]
    retry: dict[str, object] = Field(json_schema_extra=_PAYMENT_RETRY_SCHEMA)


class PaymentConfirmRequest(BaseModel):
    payment_id: object | None = Field(default=None, alias="paymentId")
    payment_key: object | None = Field(default=None, alias="paymentKey")
    order_id: object | None = Field(default=None, alias="orderId")
    amount: object | None = None


class PaymentConfirmResponse(BaseModel):
    checkout_id: str = Field(alias="checkoutId")
    payment_id: str = Field(alias="paymentId")
    order_id: str = Field(alias="orderId")
    attempt_no: int = Field(alias="attemptNo")
    payment_key: str = Field(alias="paymentKey")
    status: str
    amount: int
    currency: str
    approved_at: datetime = Field(alias="approvedAt")
    receipt_url: str | None = Field(alias="receiptUrl")
    method: str


class PaymentCancelRequest(BaseModel):
    cancel_amount: object | None = Field(default=None, alias="cancelAmount")
    cancel_reason: object | None = Field(default=None, alias="cancelReason")
    reason_message: object | None = Field(default=None, alias="reasonMessage")
    refund_bank_account: object | None = Field(
        default=None,
        alias="refundBankAccount",
    )


class PaymentCancelResponse(BaseModel):
    payment_id: str = Field(alias="paymentId")
    payment_key: str = Field(alias="paymentKey")
    status: str
    paid_amount: int = Field(alias="paidAmount")
    canceled_amount: int = Field(alias="canceledAmount")
    cancelable_amount: int = Field(alias="cancelableAmount")
    latest_cancel: dict[str, object] = Field(
        alias="latestCancel",
        json_schema_extra=_PAYMENT_LATEST_CANCEL_SCHEMA,
    )
    cancel_history: list[dict[str, object]] = Field(alias="cancelHistory")


def payment_order_response(result: PaymentOrderResult) -> PaymentOrderResponse:
    return PaymentOrderResponse(
        checkoutId=result.checkout_id,
        paymentId=result.payment_id,
        orderId=result.order_id,
        attemptNo=result.attempt_no,
        orderName=result.order_name,
        amount=result.amount,
        currency=result.currency,
        customerKey=result.customer_key,
        clientKey=result.client_key,
        successUrl=result.success_url,
        failUrl=result.fail_url,
        status=result.status,
        expiresAt=result.expires_at,
    )


def payment_auth_failure_response(
    result: PaymentAuthFailureResult,
) -> PaymentAuthFailureResponse:
    return PaymentAuthFailureResponse(
        checkoutId=result.checkout_id,
        paymentId=result.payment_id,
        orderId=result.order_id,
        status=result.status,
        failure=result.failure,
        retry=result.retry,
    )


def payment_confirm_response(result: PaymentConfirmResult) -> PaymentConfirmResponse:
    return PaymentConfirmResponse(
        checkoutId=result.checkout_id,
        paymentId=result.payment_id,
        orderId=result.order_id,
        attemptNo=result.attempt_no,
        paymentKey=result.payment_key,
        status=result.status,
        amount=result.amount,
        currency=result.currency,
        approvedAt=result.approved_at,
        receiptUrl=result.receipt_url,
        method=result.method,
    )


def payment_cancel_response(result: PaymentCancelResult) -> PaymentCancelResponse:
    return PaymentCancelResponse(
        paymentId=result.payment_id,
        paymentKey=result.payment_key,
        status=result.status,
        paidAmount=result.paid_amount,
        canceledAmount=result.canceled_amount,
        cancelableAmount=result.cancelable_amount,
        latestCancel=result.latest_cancel,
        cancelHistory=result.cancel_history,
    )


def payment_detail_response(detail: PaymentDetail) -> PaymentDetailResponse:
    return PaymentDetailResponse(
        checkoutId=detail.checkout_id,
        paymentId=detail.payment_id,
        orderId=detail.order_id,
        attemptNo=detail.attempt_no,
        status=detail.status,
        amount=detail.amount,
        currency=detail.currency,
        orderName=detail.order_name,
        approvedAt=detail.approved_at,
        receiptUrl=detail.receipt_url,
        method=detail.method,
        methodDetail=_method_detail_response(detail.method_detail),
        failure=detail.failure,
        retry=detail.retry,
    )


def _method_detail_response(
    method_detail: dict[str, object] | None,
) -> dict[str, object] | None:
    if method_detail is None:
        return None
    response = dict(method_detail)
    masked_number = response.pop("maskedCardNumber", None)
    if masked_number is not None and "maskedNumber" not in response:
        response["maskedNumber"] = masked_number
    return response
