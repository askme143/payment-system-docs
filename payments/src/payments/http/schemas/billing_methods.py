from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from payments.application.billing_methods import (
    BillingMethodList,
    BillingMethodListItem,
    DeleteBillingMethodResult,
    SetDefaultBillingMethodResult,
)


class BillingMethodItemResponse(BaseModel):
    billing_method_id: str = Field(alias="billingMethodId")
    status: str
    is_default: bool = Field(alias="isDefault")
    method: str
    card_company: str = Field(alias="cardCompany")
    masked_card_number: str = Field(alias="maskedCardNumber")
    billing_key_status: str = Field(alias="billingKeyStatus")
    deletable: bool
    delete_block_reason: str | None = Field(alias="deleteBlockReason")
    created_at: datetime = Field(alias="createdAt")


class BillingMethodListResponse(BaseModel):
    default_billing_method_id: str | None = Field(alias="defaultBillingMethodId")
    active_subscription_count: int = Field(alias="activeSubscriptionCount")
    items: list[BillingMethodItemResponse]


class SetDefaultBillingMethodResponse(BaseModel):
    billing_method_id: str = Field(alias="billingMethodId")
    is_default: bool = Field(alias="isDefault")
    previous_default_billing_method_id: str | None = Field(
        alias="previousDefaultBillingMethodId"
    )
    default_changed_at: datetime = Field(alias="defaultChangedAt")
    applies_to: str = Field(alias="appliesTo")


class DeleteBillingMethodResponse(BaseModel):
    billing_method_id: str = Field(alias="billingMethodId")
    status: str
    deleted_at: datetime = Field(alias="deletedAt")
    remaining_active_method_count: int = Field(alias="remainingActiveMethodCount")
    default_billing_method_id: str | None = Field(alias="defaultBillingMethodId")


def billing_method_list_response(
    result: BillingMethodList,
) -> BillingMethodListResponse:
    return BillingMethodListResponse(
        defaultBillingMethodId=result.default_billing_method_id,
        activeSubscriptionCount=result.active_subscription_count,
        items=[_billing_method_item_response(item) for item in result.items],
    )


def set_default_billing_method_response(
    result: SetDefaultBillingMethodResult,
) -> SetDefaultBillingMethodResponse:
    return SetDefaultBillingMethodResponse(
        billingMethodId=result.billing_method_id,
        isDefault=result.is_default,
        previousDefaultBillingMethodId=result.previous_default_billing_method_id,
        defaultChangedAt=result.default_changed_at,
        appliesTo=result.applies_to,
    )


def delete_billing_method_response(
    result: DeleteBillingMethodResult,
) -> DeleteBillingMethodResponse:
    return DeleteBillingMethodResponse(
        billingMethodId=result.billing_method_id,
        status=result.status,
        deletedAt=result.deleted_at,
        remainingActiveMethodCount=result.remaining_active_method_count,
        defaultBillingMethodId=result.default_billing_method_id,
    )


def _billing_method_item_response(
    item: BillingMethodListItem,
) -> BillingMethodItemResponse:
    return BillingMethodItemResponse(
        billingMethodId=item.billing_method_id,
        status=item.status,
        isDefault=item.is_default,
        method=item.method,
        cardCompany=item.card_company,
        maskedCardNumber=item.masked_card_number,
        billingKeyStatus=item.billing_key_status,
        deletable=item.deletable,
        deleteBlockReason=item.delete_block_reason,
        createdAt=item.created_at,
    )
