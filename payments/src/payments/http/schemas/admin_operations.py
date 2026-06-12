from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from payments.application.admin_operations import (
    AdminPage,
    AdminPaymentCancelResult,
    AdminPaymentListItem,
    AdminPaymentListResult,
    AdminSubscriptionAdjustResult,
    AdminSubscriptionListItem,
    AdminSubscriptionListResult,
)


class AdminPageResponse(BaseModel):
    next_cursor: str | None = Field(alias="nextCursor")
    has_more: bool = Field(alias="hasMore")


class AdminPaymentListItemResponse(BaseModel):
    payment_id: str = Field(alias="paymentId")
    checkout_id: str | None = Field(alias="checkoutId")
    user_id: str | None = Field(alias="userId")
    user_email: str | None = Field(alias="userEmail")
    order_id: str = Field(alias="orderId")
    order_name: str = Field(alias="orderName")
    payment_key: str | None = Field(alias="paymentKey")
    status: str
    amount: int
    paid_amount: int = Field(alias="paidAmount")
    cancelable_amount: int = Field(alias="cancelableAmount")
    currency: str
    approved_at: datetime | None = Field(alias="approvedAt")
    method_summary: str | None = Field(alias="methodSummary")
    detail_url: str = Field(alias="detailUrl")
    cancel_url: str | None = Field(alias="cancelUrl")


class AdminPaymentListResponse(BaseModel):
    items: list[AdminPaymentListItemResponse]
    page: AdminPageResponse


class AdminPaymentCancelRequest(BaseModel):
    cancel_amount: object | None = Field(default=None, alias="cancelAmount")
    cancel_reason: object | None = Field(default=None, alias="cancelReason")
    reason_message: object | None = Field(default=None, alias="reasonMessage")
    notify_customer: object = Field(default=True, alias="notifyCustomer")


class AdminPaymentCancelResponse(BaseModel):
    payment_id: str = Field(alias="paymentId")
    status: str
    paid_amount: int = Field(alias="paidAmount")
    canceled_amount: int = Field(alias="canceledAmount")
    cancelable_amount: int = Field(alias="cancelableAmount")
    operator_audit_id: str = Field(alias="operatorAuditId")
    cancel_history: list[dict[str, object]] = Field(alias="cancelHistory")


class AdminSubscriptionAdjustPostponeByRequest(BaseModel):
    days: object | None = None


class AdminSubscriptionAdjustRequest(BaseModel):
    adjustment_type: object | None = Field(default=None, alias="adjustmentType")
    payment_key: object | None = Field(default=None, alias="paymentKey")
    invoice_id: object | None = Field(default=None, alias="invoiceId")
    postpone_by: AdminSubscriptionAdjustPostponeByRequest | None = Field(
        default=None,
        alias="postponeBy",
    )
    next_billing_at: object | None = Field(default=None, alias="nextBillingAt")
    target_status: object | None = Field(default=None, alias="targetStatus")
    reason_code: object | None = Field(default=None, alias="reasonCode")
    reason_message: object | None = Field(default=None, alias="reasonMessage")
    notify_customer: object = Field(default=False, alias="notifyCustomer")


class AdminSubscriptionAdjustResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    adjustment_type: str = Field(alias="adjustmentType")
    previous_state: dict[str, object] = Field(alias="previousState")
    current_state: dict[str, object] = Field(alias="currentState")
    operator_audit_id: str = Field(alias="operatorAuditId")
    notified_customer: bool = Field(alias="notifiedCustomer")


class AdminSubscriptionListItemResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    user_id: str = Field(alias="userId")
    user_email: str | None = Field(alias="userEmail")
    product_code: str = Field(alias="productCode")
    product_name: str = Field(alias="productName")
    plan_id: str = Field(alias="planId")
    plan_name: str = Field(alias="planName")
    status: str
    current_period_start_at: datetime | None = Field(alias="currentPeriodStart")
    current_period_end_at: datetime | None = Field(alias="currentPeriodEnd")
    next_billing_at: datetime | None = Field(alias="nextBillingAt")
    payment_failure: dict[str, object] | None = Field(alias="paymentFailure")
    default_billing_method_summary: str | None = Field(
        alias="defaultBillingMethodSummary"
    )
    detail_url: str = Field(alias="detailUrl")
    adjust_url: str | None = Field(alias="adjustUrl")


class AdminSubscriptionListResponse(BaseModel):
    items: list[AdminSubscriptionListItemResponse]
    page: AdminPageResponse


def admin_payment_list_response(
    result: AdminPaymentListResult,
) -> AdminPaymentListResponse:
    return AdminPaymentListResponse(
        items=[_payment_item_response(item) for item in result.items],
        page=_page_response(result.page),
    )


def admin_payment_cancel_response(
    result: AdminPaymentCancelResult,
) -> AdminPaymentCancelResponse:
    return AdminPaymentCancelResponse(
        paymentId=result.payment_id,
        status=result.status,
        paidAmount=result.paid_amount,
        canceledAmount=result.canceled_amount,
        cancelableAmount=result.cancelable_amount,
        operatorAuditId=result.operator_audit_id,
        cancelHistory=result.cancel_history,
    )


def admin_subscription_adjust_response(
    result: AdminSubscriptionAdjustResult,
) -> AdminSubscriptionAdjustResponse:
    return AdminSubscriptionAdjustResponse(
        subscriptionId=result.subscription_id,
        adjustmentType=result.adjustment_type,
        previousState=result.previous_state,
        currentState=result.current_state,
        operatorAuditId=result.operator_audit_id,
        notifiedCustomer=result.notified_customer,
    )


def admin_subscription_list_response(
    result: AdminSubscriptionListResult,
) -> AdminSubscriptionListResponse:
    return AdminSubscriptionListResponse(
        items=[_subscription_item_response(item) for item in result.items],
        page=_page_response(result.page),
    )


def _page_response(page: AdminPage) -> AdminPageResponse:
    return AdminPageResponse(
        nextCursor=page.next_cursor,
        hasMore=page.has_more,
    )


def _payment_item_response(
    item: AdminPaymentListItem,
) -> AdminPaymentListItemResponse:
    return AdminPaymentListItemResponse(
        paymentId=item.payment_id,
        checkoutId=item.checkout_id,
        userId=item.user_id,
        userEmail=item.user_email,
        orderId=item.order_id,
        orderName=item.order_name,
        paymentKey=item.payment_key,
        status=item.status,
        amount=item.amount,
        paidAmount=item.paid_amount,
        cancelableAmount=item.cancelable_amount,
        currency=item.currency,
        approvedAt=item.approved_at,
        methodSummary=item.method_summary,
        detailUrl=item.detail_url,
        cancelUrl=item.cancel_url,
    )


def _subscription_item_response(
    item: AdminSubscriptionListItem,
) -> AdminSubscriptionListItemResponse:
    return AdminSubscriptionListItemResponse(
        subscriptionId=item.subscription_id,
        userId=item.user_id,
        userEmail=item.user_email,
        productCode=item.product_code,
        productName=item.product_name,
        planId=item.plan_id,
        planName=item.plan_name,
        status=item.status,
        currentPeriodStart=item.current_period_start_at,
        currentPeriodEnd=item.current_period_end_at,
        nextBillingAt=item.next_billing_at,
        paymentFailure=item.payment_failure,
        defaultBillingMethodSummary=item.default_billing_method_summary,
        detailUrl=item.detail_url,
        adjustUrl=item.adjust_url,
    )
