from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Header

from payments.application.errors import AuthenticationError
from payments.application.webhooks import receive_toss_payment_webhook
from payments.http.dependencies import HttpDependencies
from payments.http.schemas.webhooks import (
    TossPaymentWebhookRequest,
    TossPaymentWebhookResponse,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post(
        "/toss-payments",
        response_model=TossPaymentWebhookResponse,
    )
    async def receive_toss_payment(
        request: TossPaymentWebhookRequest,
        toss_signature: Annotated[
            str | None, Header(alias="Toss-Signature")
        ] = None,
    ) -> TossPaymentWebhookResponse:
        if not dependencies.toss_webhook_secret or not toss_signature:
            raise AuthenticationError("toss webhook signature is required")
        if not hmac.compare_digest(
            toss_signature,
            dependencies.toss_webhook_secret,
        ):
            raise AuthenticationError("toss webhook signature is invalid")
        result = await receive_toss_payment_webhook(
            request.model_dump(mode="json", by_alias=True, exclude_none=True),
            dependencies.webhooks,
            dependencies.clock,
            dependencies.webhook_uow_factory,
        )
        return TossPaymentWebhookResponse(received=result.received)

    return router
