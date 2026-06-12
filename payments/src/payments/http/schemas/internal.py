from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from payments.application.jobs.billing_retry import BillingRetryResult
from payments.application.jobs.subscription_billing import (
    SubscriptionBillingRunResult,
)
from payments.application.jobs.subscription_expiration import (
    SubscriptionExpirationRunSummary,
)

_INTERNAL_RETRY_NOTIFICATION_SCHEMA = {
    "properties": {
        "template": {"type": "string"},
        "queued": {"type": "boolean"},
        "payload": {
            "type": "object",
            "properties": {
                "invoiceId": {"type": "string"},
                "amount": {"type": "integer"},
                "billingDate": {"type": "string"},
                "receiptUrl": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
    },
    "required": ["template", "queued", "payload"],
    "additionalProperties": False,
}


class InternalBillingRunRequest(BaseModel):
    job_type: Literal["billing", "reminder", "cancel_expiration"] = Field(
        default="billing",
        alias="jobType",
    )
    billing_date: date | None = Field(default=None, alias="billingDate")
    limit: int = Field(default=100, ge=1, le=500, strict=True)
    dry_run: bool = Field(default=False, alias="dryRun", strict=True)


class InternalBillingRunResponse(BaseModel):
    job_type: str | None = Field(default=None, alias="jobType")
    billing_date: date | None = Field(default=None, alias="billingDate")
    selected: int | None = None
    processed: int | None = None
    paid: int | None = None
    failed: int | None = None
    skipped: int | None = None
    excluded_cancel_scheduled: int | None = Field(
        default=None,
        alias="excludedCancelScheduled",
    )
    reminder_emails_sent: int | None = Field(
        default=None,
        alias="reminderEmailsSent",
    )
    success_emails_queued: int | None = Field(
        default=None,
        alias="successEmailsQueued",
    )
    failure_emails_queued: int | None = Field(
        default=None,
        alias="failureEmailsQueued",
    )
    cancel_expiration_emails_queued: int | None = Field(
        default=None,
        alias="cancelExpirationEmailsQueued",
    )
    expired_subscription_ids: list[str] | None = Field(
        default=None,
        alias="expiredSubscriptionIds",
    )


class InternalBillingRetryRequest(BaseModel):
    force: bool = Field(default=False, strict=True)
    reason: str | None = None
    dry_run: bool = Field(default=False, alias="dryRun", strict=True)


class InternalBillingRetryResponse(BaseModel):
    invoice_id: str = Field(alias="invoiceId")
    subscription_id: str = Field(alias="subscriptionId")
    status: str
    invoice_status: str = Field(alias="invoiceStatus")
    payment_status: str = Field(alias="paymentStatus")
    next_billing_date: date | None = Field(alias="nextBillingDate")
    receipt_url: str | None = Field(alias="receiptUrl")
    notification: dict[str, object] = Field(
        json_schema_extra=_INTERNAL_RETRY_NOTIFICATION_SCHEMA,
    )


def subscription_expiration_response(
    summary: SubscriptionExpirationRunSummary,
) -> InternalBillingRunResponse:
    return InternalBillingRunResponse(
        jobType="cancel_expiration",
        selected=summary.selected_count,
        processed=summary.processed_count,
        skipped=summary.skipped_count,
        failed=summary.failed_count,
        cancelExpirationEmailsQueued=summary.cancel_expiration_emails_queued,
        expiredSubscriptionIds=summary.expired_subscription_ids,
    )


def subscription_billing_response(
    result: SubscriptionBillingRunResult,
) -> InternalBillingRunResponse:
    return InternalBillingRunResponse(
        billingDate=result.billing_date,
        processed=result.processed,
        paid=result.paid,
        failed=result.failed,
        skipped=result.skipped,
        excludedCancelScheduled=result.excluded_cancel_scheduled,
        reminderEmailsSent=result.reminder_emails_sent,
        successEmailsQueued=result.success_emails_queued,
        failureEmailsQueued=result.failure_emails_queued,
    )


def billing_retry_response(result: BillingRetryResult) -> InternalBillingRetryResponse:
    return InternalBillingRetryResponse(
        invoiceId=result.invoice_id,
        subscriptionId=result.subscription_id,
        status=result.status,
        invoiceStatus=result.invoice_status,
        paymentStatus=result.payment_status,
        nextBillingDate=_response_date(result.next_billing_date),
        receiptUrl=result.receipt_url,
        notification=result.notification,
    )


def _response_date(value: datetime | None) -> date | None:
    return value.date() if value is not None else None
