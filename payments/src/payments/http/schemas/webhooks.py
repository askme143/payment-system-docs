from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TossPaymentWebhookRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_type: str | None = Field(default=None, alias="eventType")
    event_id: str | None = Field(default=None, alias="eventId")
    payment_key: str | None = Field(default=None, alias="paymentKey")
    order_id: str | None = Field(default=None, alias="orderId")
    status: str | None = None
    approved_at: datetime | None = Field(default=None, alias="approvedAt")


class TossPaymentWebhookResponse(BaseModel):
    received: bool
