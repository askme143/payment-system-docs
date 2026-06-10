from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator

from payments.application.payment_orders import (
    PaymentDetail,
    PaymentOrderItem,
    PaymentOrderResult,
)


class PaymentOrderItemRequest(BaseModel):
    sku_id: str = Field(alias="skuId")
    quantity: int

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("quantity must be greater than or equal to 1")
        return value


class CreatePaymentOrderRequest(BaseModel):
    items: list[PaymentOrderItemRequest]
    success_url: HttpUrl = Field(alias="successUrl")
    fail_url: HttpUrl = Field(alias="failUrl")
    checkout_id: str | None = Field(default=None, alias="checkoutId")

    @field_validator("items")
    @classmethod
    def items_must_not_be_empty(
        cls,
        value: list[PaymentOrderItemRequest],
    ) -> list[PaymentOrderItemRequest]:
        if not value:
            raise ValueError("items must not be empty")
        return value

    def to_application_items(self) -> list[PaymentOrderItem]:
        return [
            PaymentOrderItem(sku_id=item.sku_id, quantity=item.quantity)
            for item in self.items
        ]


class PaymentOrderResponse(BaseModel):
    checkout_id: str = Field(alias="checkoutId")
    payment_id: str = Field(alias="paymentId")
    order_id: str = Field(alias="orderId")
    amount: int
    status: str


class PaymentDetailResponse(BaseModel):
    id: str
    order_id: str = Field(alias="orderId")
    amount: int
    status: str
    checkout_id: str | None = Field(alias="checkoutId")
    approved_at: datetime | None = Field(alias="approvedAt")
    receipt_url: str | None = Field(alias="receiptUrl")


def payment_order_response(result: PaymentOrderResult) -> PaymentOrderResponse:
    return PaymentOrderResponse(
        checkoutId=result.checkout_id,
        paymentId=result.payment_id,
        orderId=result.order_id,
        amount=result.amount,
        status=result.status,
    )


def payment_detail_response(detail: PaymentDetail) -> PaymentDetailResponse:
    return PaymentDetailResponse(
        id=detail.id,
        orderId=detail.order_id,
        amount=detail.amount,
        status=detail.status,
        checkoutId=detail.checkout_id,
        approvedAt=detail.approved_at,
        receiptUrl=detail.receipt_url,
    )
