from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from payments.application.context import RequestContext
from payments.application.invoices import get_invoice_detail, list_user_invoices
from payments.http.dependencies import HttpDependencies, request_context_dependency
from payments.http.schemas.invoices import (
    InvoiceDetailResponse,
    InvoiceListResponse,
    invoice_detail_response,
    invoice_list_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(tags=["invoices"])
    require_user_context = request_context_dependency(
        dependencies.internal_service_token,
        True,
    )

    @router.get("/invoices", response_model=InvoiceListResponse)
    async def list_invoices(
        status: str | None = None,
        paymentStatus: str | None = None,
        subscriptionId: str | None = None,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        cursor: str | None = None,
        limit: str = "20",
        ctx: RequestContext = Depends(require_user_context),
    ) -> InvoiceListResponse:
        result = await list_user_invoices(
            requester=ctx,
            invoices=dependencies.invoices,
            limit=limit,
            status=status,
            payment_status=paymentStatus,
            subscription_id=subscriptionId,
            from_date=from_,
            to_date=to,
            cursor=cursor,
        )
        return invoice_list_response(result)

    @router.get("/invoices/{invoiceId}", response_model=InvoiceDetailResponse)
    async def get_invoice(
        invoiceId: str,
        ctx: RequestContext = Depends(require_user_context),
    ) -> InvoiceDetailResponse:
        detail = await get_invoice_detail(
            requester=ctx,
            invoice_id=invoiceId,
            invoices=dependencies.invoices,
        )
        return invoice_detail_response(detail)

    return router
