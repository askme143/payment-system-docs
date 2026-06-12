from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from payments.application.invoices import InvoiceDetail, InvoiceList, InvoiceListItem


class InvoiceListItemResponse(BaseModel):
    invoice_id: str = Field(alias="invoiceId")
    subscription_id: str | None = Field(alias="subscriptionId")
    product_name: str = Field(alias="productName")
    plan_name: str = Field(alias="planName")
    invoice_type: str = Field(alias="invoiceType")
    status: str
    payment_status: str | None = Field(
        alias="paymentStatus",
        exclude_if=lambda value: value is None,
    )
    amount: int
    currency: str
    billing_date: date = Field(alias="billingDate")
    paid_at: datetime | None = Field(alias="paidAt")
    receipt_available: bool = Field(alias="receiptAvailable")
    failure_summary: str | None = Field(alias="failureSummary")
    detail_url: str = Field(alias="detailUrl")


class InvoicePageResponse(BaseModel):
    limit: int
    next_cursor: str | None = Field(alias="nextCursor")


class InvoiceListResponse(BaseModel):
    items: list[InvoiceListItemResponse]
    page: InvoicePageResponse


class InvoiceFailureResponse(BaseModel):
    code: str
    message: str
    retryable: bool


class InvoiceRetryResponse(BaseModel):
    available: bool
    scheduled_at: datetime | None = Field(alias="scheduledAt")


class InvoiceActionsResponse(BaseModel):
    billing_method_update_url: str | None = Field(alias="billingMethodUpdateUrl")
    subscription_manage_url: str = Field(alias="subscriptionManageUrl")


class InvoiceDetailResponse(BaseModel):
    invoice_id: str = Field(alias="invoiceId")
    subscription_id: str | None = Field(alias="subscriptionId")
    subscription_status: str | None = Field(alias="subscriptionStatus")
    status: str
    payment_status: str | None = Field(alias="paymentStatus")
    amount: int
    currency: str
    billing_date: date = Field(alias="billingDate")
    paid_at: datetime | None = Field(alias="paidAt")
    receipt_url: str | None = Field(alias="receiptUrl")
    failure: InvoiceFailureResponse | None
    retry: InvoiceRetryResponse
    actions: InvoiceActionsResponse


def invoice_list_response(result: InvoiceList) -> InvoiceListResponse:
    return InvoiceListResponse(
        items=[_invoice_list_item_response(item) for item in result.items],
        page=InvoicePageResponse(
            limit=result.page.limit,
            nextCursor=result.page.next_cursor,
        ),
    )


def invoice_detail_response(detail: InvoiceDetail) -> InvoiceDetailResponse:
    return InvoiceDetailResponse(
        invoiceId=detail.invoice_id,
        subscriptionId=detail.subscription_id,
        subscriptionStatus=detail.subscription_status,
        status=detail.status,
        paymentStatus=detail.payment_status,
        amount=detail.amount,
        currency=detail.currency,
        billingDate=detail.billing_date,
        paidAt=detail.paid_at,
        receiptUrl=detail.receipt_url,
        failure=(
            InvoiceFailureResponse(
                code=detail.failure.code,
                message=detail.failure.message,
                retryable=detail.failure.retryable,
            )
            if detail.failure is not None
            else None
        ),
        retry=InvoiceRetryResponse(
            available=detail.retry.available,
            scheduledAt=detail.retry.scheduled_at,
        ),
        actions=InvoiceActionsResponse(
            billingMethodUpdateUrl=detail.actions.billing_method_update_url,
            subscriptionManageUrl=detail.actions.subscription_manage_url,
        ),
    )


def _invoice_list_item_response(
    item: InvoiceListItem,
) -> InvoiceListItemResponse:
    return InvoiceListItemResponse(
        invoiceId=item.invoice_id,
        subscriptionId=item.subscription_id,
        productName=item.product_name,
        planName=item.plan_name,
        invoiceType=item.invoice_type,
        status=item.status,
        paymentStatus=item.payment_status,
        amount=item.amount,
        currency=item.currency,
        billingDate=item.billing_date,
        paidAt=item.paid_at,
        receiptAvailable=item.receipt_available,
        failureSummary=item.failure_summary,
        detailUrl=item.detail_url,
    )
