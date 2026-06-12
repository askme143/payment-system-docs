from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from payments.application.subscription_changes import (
    SubscriptionChangePreviewResult,
    SubscriptionChangeResult,
)
from payments.application.subscription_checkout import (
    SubscriptionCheckoutResult,
    SubscriptionConfirmResult,
)
from payments.application.subscriptions import (
    CurrentUserSubscription,
    CurrentUserSubscriptions,
    SubscriptionMutationResult,
)

_SUBSCRIPTION_IMMEDIATE_PAYMENT_SCHEMA = {
    "properties": {
        "amount": {"type": "integer"},
        "currency": {"type": "string"},
        "invoiceType": {"type": "string"},
    },
    "required": ["amount", "currency", "invoiceType"],
    "additionalProperties": False,
}
_SUBSCRIPTION_CHANGE_PAYMENT_SCHEMA = {
    "properties": {
        "invoiceId": {"type": ["string", "null"]},
        "paymentId": {"type": ["string", "null"]},
        "status": {"type": "string"},
        "amount": {"type": "integer"},
        "currency": {"type": "string"},
        "receiptUrl": {"type": ["string", "null"]},
    },
    "required": ["invoiceId", "paymentId", "status", "amount", "currency"],
    "additionalProperties": True,
}
_SUBSCRIPTION_CHANGE_NOTIFICATION_SCHEMA = {
    "properties": {
        "template": {"type": "string"},
        "queued": {"type": "boolean"},
    },
    "required": ["template", "queued"],
    "additionalProperties": True,
}
_SUBSCRIPTION_PENDING_PLAN_SCHEMA = {
    "properties": {
        "planId": {"type": "string"},
        "planName": {"type": "string"},
        "effectiveAt": {"type": "string", "format": "date"},
    },
    "required": ["planId", "planName", "effectiveAt"],
    "additionalProperties": False,
}


class CurrentUserSubscriptionResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    product_code: str = Field(alias="productCode")
    plan_id: str = Field(alias="planId")
    plan_name: str = Field(alias="planName")
    status: str
    current_period_start: date | None = Field(alias="currentPeriodStart")
    current_period_end: date | None = Field(alias="currentPeriodEnd")
    next_billing_date: date | None = Field(alias="nextBillingDate")
    resume_available: bool = Field(alias="resumeAvailable")
    resubscribe_url: str | None = Field(alias="resubscribeUrl")


class DefaultBillingMethodResponse(BaseModel):
    billing_method_id: str = Field(alias="billingMethodId")
    is_default: bool = Field(alias="isDefault")
    display_name: str = Field(alias="displayName")


class CurrentUserSubscriptionsResponse(BaseModel):
    subscriptions: list[CurrentUserSubscriptionResponse]
    billing_method: DefaultBillingMethodResponse | None = Field(alias="billingMethod")


class CancelSubscriptionRequest(BaseModel):
    cancel_reason: str | None = Field(default=None, alias="cancelReason")
    feedback: str | None = None


class ResumeSubscriptionRequest(BaseModel):
    resume_reason: str | None = Field(default=None, alias="resumeReason")


class SubscriptionMutationResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    status: str
    cancel_at: date | None = Field(alias="cancelAt")
    current_period_end: date | None = Field(alias="currentPeriodEnd")
    next_billing_date: date | None = Field(alias="nextBillingDate")
    access_until: date | None = Field(alias="accessUntil")
    resume_available: bool = Field(alias="resumeAvailable")


class SubscriptionResumeResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    status: str
    cancel_at: date | None = Field(alias="cancelAt")
    current_period_end: date | None = Field(alias="currentPeriodEnd")
    next_billing_date: date | None = Field(alias="nextBillingDate")
    resume_available: bool = Field(alias="resumeAvailable")


class SubscriptionCheckoutRequest(BaseModel):
    plan_id: object | None = Field(default=None, alias="planId")
    success_url: object | None = Field(default=None, alias="successUrl")
    fail_url: object | None = Field(default=None, alias="failUrl")


class SubscriptionCheckoutResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    customer_key: str = Field(alias="customerKey")
    product_code: str = Field(alias="productCode")
    amount: int
    currency: str
    order_name: str = Field(alias="orderName")
    client_key: str = Field(alias="clientKey")
    success_url: str = Field(alias="successUrl")
    fail_url: str = Field(alias="failUrl")


class SubscriptionConfirmRequest(BaseModel):
    subscription_id: object | None = Field(default=None, alias="subscriptionId")
    customer_key: object | None = Field(default=None, alias="customerKey")
    auth_key: object | None = Field(default=None, alias="authKey")


class SubscriptionConfirmResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    status: str
    payment_status: str = Field(alias="paymentStatus")
    payment_id: str = Field(alias="paymentId")
    invoice_id: str = Field(alias="invoiceId")
    next_billing_date: date | None = Field(alias="nextBillingDate")


_FORBIDDEN_CHANGE_DATE_FIELDS = frozenset(
    {"nextBillingDate", "billingDate", "billingDay"}
)


class SubscriptionChangePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    target_plan_id: object | None = Field(default=None, alias="targetPlanId")

    def forbidden_date_fields(self) -> set[str]:
        return _forbidden_change_date_fields(self)


class SubscriptionChangePreviewResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    product_code: str = Field(alias="productCode")
    current_plan_id: str = Field(alias="currentPlanId")
    target_plan_id: str = Field(alias="targetPlanId")
    server_decision: str = Field(alias="serverDecision")
    will_apply: str = Field(alias="willApply")
    confirmation_token: str = Field(alias="confirmationToken")
    confirmation_expires_at: datetime = Field(alias="confirmationExpiresAt")
    immediate_payment: dict[str, object] | None = Field(
        alias="immediatePayment",
        json_schema_extra=_SUBSCRIPTION_IMMEDIATE_PAYMENT_SCHEMA,
    )
    effective_at: date | None = Field(default=None, alias="effectiveAt")
    next_billing_date: date | None = Field(alias="nextBillingDate")
    notice: str


class SubscriptionChangeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    confirmation_token: object | None = Field(default=None, alias="confirmationToken")
    confirmed: object | None = None

    def forbidden_date_fields(self) -> set[str]:
        return _forbidden_change_date_fields(self)


class SubscriptionChangeResponse(BaseModel):
    subscription_id: str = Field(alias="subscriptionId")
    product_code: str = Field(alias="productCode")
    status: str
    server_decision: str = Field(alias="serverDecision")
    plan_id: str = Field(alias="planId")
    previous_plan_id: str = Field(alias="previousPlanId")
    applied_at: datetime | None = Field(alias="appliedAt")
    next_billing_date: date | None = Field(alias="nextBillingDate")
    payment: dict[str, object] | None = Field(
        json_schema_extra=_SUBSCRIPTION_CHANGE_PAYMENT_SCHEMA,
    )
    notification: dict[str, object] | None = Field(
        default=None,
        json_schema_extra=_SUBSCRIPTION_CHANGE_NOTIFICATION_SCHEMA,
    )
    pending_plan: dict[str, object] | None = Field(
        alias="pendingPlan",
        json_schema_extra=_SUBSCRIPTION_PENDING_PLAN_SCHEMA,
    )


def current_user_subscriptions_response(
    account: CurrentUserSubscriptions,
) -> CurrentUserSubscriptionsResponse:
    billing_method = account.billing_method
    return CurrentUserSubscriptionsResponse(
        subscriptions=[
            _current_user_subscription_response(subscription)
            for subscription in account.subscriptions
        ],
        billingMethod=(
            DefaultBillingMethodResponse(
                billingMethodId=billing_method.billing_method_id,
                isDefault=billing_method.is_default,
                displayName=billing_method.display_name,
            )
            if billing_method is not None
            else None
        ),
    )


def subscription_mutation_response(
    result: SubscriptionMutationResult,
) -> SubscriptionMutationResponse:
    return SubscriptionMutationResponse(
        subscriptionId=result.subscription_id,
        status=result.status,
        cancelAt=_response_date(result.cancel_at),
        currentPeriodEnd=_response_date(result.current_period_end_at),
        nextBillingDate=_response_date(result.next_billing_at),
        accessUntil=_response_date(result.access_until),
        resumeAvailable=result.resume_available,
    )


def subscription_resume_response(
    result: SubscriptionMutationResult,
) -> SubscriptionResumeResponse:
    return SubscriptionResumeResponse(
        subscriptionId=result.subscription_id,
        status=result.status,
        cancelAt=_response_date(result.cancel_at),
        currentPeriodEnd=_response_date(result.current_period_end_at),
        nextBillingDate=_response_date(result.next_billing_at),
        resumeAvailable=result.resume_available,
    )


def subscription_checkout_response(
    result: SubscriptionCheckoutResult,
) -> SubscriptionCheckoutResponse:
    return SubscriptionCheckoutResponse(
        subscriptionId=result.subscription_id,
        customerKey=result.customer_key,
        productCode=result.product_code,
        amount=result.amount,
        currency=result.currency,
        orderName=result.order_name,
        clientKey=result.client_key,
        successUrl=result.success_url,
        failUrl=result.fail_url,
    )


def subscription_confirm_response(
    result: SubscriptionConfirmResult,
) -> SubscriptionConfirmResponse:
    return SubscriptionConfirmResponse(
        subscriptionId=result.subscription_id,
        status=result.status,
        paymentStatus=result.payment_status,
        paymentId=result.payment_id,
        invoiceId=result.invoice_id,
        nextBillingDate=_response_date(result.next_billing_date),
    )


def subscription_change_preview_response(
    result: SubscriptionChangePreviewResult,
) -> SubscriptionChangePreviewResponse:
    return SubscriptionChangePreviewResponse(
        subscriptionId=result.subscription_id,
        productCode=result.product_code,
        currentPlanId=result.current_plan_id,
        targetPlanId=result.target_plan_id,
        serverDecision=result.server_decision,
        willApply=result.will_apply,
        confirmationToken=result.confirmation_token,
        confirmationExpiresAt=result.confirmation_expires_at,
        immediatePayment=result.immediate_payment,
        effectiveAt=_response_date(result.effective_at),
        nextBillingDate=_response_date(result.next_billing_date),
        notice=result.notice,
    )


def subscription_change_response(
    result: SubscriptionChangeResult,
) -> SubscriptionChangeResponse:
    return SubscriptionChangeResponse(
        subscriptionId=result.subscription_id,
        productCode=result.product_code,
        status=result.status,
        serverDecision=result.server_decision,
        planId=result.plan_id,
        previousPlanId=result.previous_plan_id,
        appliedAt=result.applied_at,
        nextBillingDate=_response_date(result.next_billing_date),
        payment=result.payment,
        notification=result.notification,
        pendingPlan=_pending_plan_response(result.pending_plan),
    )


def _forbidden_change_date_fields(model: BaseModel) -> set[str]:
    extra_fields = model.model_extra or {}
    return set(extra_fields).intersection(_FORBIDDEN_CHANGE_DATE_FIELDS)


def _current_user_subscription_response(
    subscription: CurrentUserSubscription,
) -> CurrentUserSubscriptionResponse:
    return CurrentUserSubscriptionResponse(
        subscriptionId=subscription.subscription_id,
        productCode=subscription.product_code,
        planId=subscription.plan_id,
        planName=subscription.plan_name,
        status=subscription.status,
        currentPeriodStart=_response_date(subscription.current_period_start_at),
        currentPeriodEnd=_response_date(subscription.current_period_end_at),
        nextBillingDate=_response_date(subscription.next_billing_at),
        resumeAvailable=subscription.resume_available,
        resubscribeUrl=subscription.resubscribe_url,
    )


def _response_date(value: datetime | None) -> date | None:
    return value.date() if value is not None else None


def _pending_plan_response(
    pending_plan: dict[str, object] | None,
) -> dict[str, object] | None:
    if pending_plan is None:
        return None
    response = dict(pending_plan)
    effective_at = response.get("effectiveAt")
    if isinstance(effective_at, datetime):
        response["effectiveAt"] = effective_at.date()
    return response
