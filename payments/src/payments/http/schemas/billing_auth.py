from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from payments.application.billing_auth import (
    BillingAuthIssueResult,
    BillingAuthStartResult,
)


class BillingAuthStartRequest(BaseModel):
    success_url: object | None = Field(default=None, alias="successUrl")
    fail_url: object | None = Field(default=None, alias="failUrl")
    set_as_default: object = Field(default=False, alias="setAsDefault")


class BillingAuthStartResponse(BaseModel):
    billing_auth_id: str = Field(alias="billingAuthId")
    customer_key: str = Field(alias="customerKey")
    client_key: str = Field(alias="clientKey")
    success_url: str = Field(alias="successUrl")
    fail_url: str = Field(alias="failUrl")
    set_as_default: bool = Field(alias="setAsDefault")
    status: str


class BillingAuthIssueRequest(BaseModel):
    billing_auth_id: object | None = Field(default=None, alias="billingAuthId")
    auth_key: object | None = Field(default=None, alias="authKey")
    customer_key: object | None = Field(default=None, alias="customerKey")


class BillingAuthIssueResponse(BaseModel):
    billing_method_id: str = Field(alias="billingMethodId")
    status: str
    is_default: bool = Field(alias="isDefault")
    method: str
    card_company: str = Field(alias="cardCompany")
    masked_card_number: str = Field(alias="maskedCardNumber")
    billing_key_status: str = Field(alias="billingKeyStatus")
    created_at: datetime = Field(alias="createdAt")


def billing_auth_start_response(
    result: BillingAuthStartResult,
) -> BillingAuthStartResponse:
    return BillingAuthStartResponse(
        billingAuthId=result.billing_auth_id,
        customerKey=result.customer_key,
        clientKey=result.client_key,
        successUrl=result.success_url,
        failUrl=result.fail_url,
        setAsDefault=result.set_as_default,
        status=result.status,
    )


def billing_auth_issue_response(
    result: BillingAuthIssueResult,
) -> BillingAuthIssueResponse:
    return BillingAuthIssueResponse(
        billingMethodId=result.billing_method_id,
        status=result.status,
        isDefault=result.is_default,
        method=result.method,
        cardCompany=result.card_company,
        maskedCardNumber=result.masked_card_number,
        billingKeyStatus=result.billing_key_status,
        createdAt=result.created_at,
    )
