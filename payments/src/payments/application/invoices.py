from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from payments.application.context import RequestContext
from payments.application.cursors import encode_cursor
from payments.application.errors import (
    AuthorizationError,
    BadRequestError,
    ForbiddenError,
    ResourceNotFoundError,
)
from payments.application.ports.invoices import (
    InvoiceDetailRecord,
    InvoiceListRecord,
    InvoiceRepository,
    InvoiceStatus,
    PaymentStatus,
    SubscriptionStatus,
)

_QUERY_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class InvoiceListItem:
    invoice_id: str
    subscription_id: str | None
    product_name: str
    plan_name: str
    invoice_type: str
    status: InvoiceStatus
    payment_status: PaymentStatus | None
    amount: int
    currency: str
    billing_date: date
    paid_at: datetime | None
    receipt_available: bool
    failure_summary: str | None
    detail_url: str


@dataclass(frozen=True, slots=True)
class InvoicePage:
    limit: int
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class InvoiceList:
    items: list[InvoiceListItem]
    page: InvoicePage


@dataclass(frozen=True, slots=True)
class InvoiceFailure:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class InvoiceRetry:
    available: bool
    scheduled_at: datetime | None


@dataclass(frozen=True, slots=True)
class InvoiceActions:
    billing_method_update_url: str | None
    subscription_manage_url: str


@dataclass(frozen=True, slots=True)
class InvoiceDetail:
    invoice_id: str
    subscription_id: str | None
    subscription_status: SubscriptionStatus | None
    status: InvoiceStatus
    payment_status: PaymentStatus | None
    amount: int
    currency: str
    billing_date: date
    paid_at: datetime | None
    receipt_url: str | None
    failure: InvoiceFailure | None
    retry: InvoiceRetry
    actions: InvoiceActions


async def list_user_invoices(
    *,
    requester: RequestContext,
    invoices: InvoiceRepository,
    limit: int | str,
    status: InvoiceStatus | str | None = None,
    payment_status: PaymentStatus | str | None = None,
    subscription_id: str | None = None,
    from_date: date | str | None = None,
    to_date: date | str | None = None,
    cursor: str | None = None,
) -> InvoiceList:
    """현재 회원의 인보이스 목록을 조회합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        invoices: 회원 인보이스를 조회하는 저장소입니다.
        limit: 반환할 최대 항목 수입니다.

    Returns:
        인보이스 요약 목록과 페이지 정보를 담은 결과입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    parsed_limit = _parse_limit(limit)
    parsed_status = _parse_invoice_status(status)
    parsed_payment_status = _parse_payment_status(payment_status)
    parsed_from_date = _parse_query_date(from_date, "from")
    parsed_to_date = _parse_query_date(to_date, "to")
    if (
        parsed_from_date is not None
        and parsed_to_date is not None
        and parsed_from_date > parsed_to_date
    ):
        raise BadRequestError("invoice date range is invalid")

    records = await invoices.list_invoices_for_user(
        requester.user_id,
        parsed_limit + 1,
        status=parsed_status,
        payment_status=parsed_payment_status,
        subscription_id=subscription_id,
        from_date=parsed_from_date,
        to_date=parsed_to_date,
        cursor=cursor,
    )
    page_records = records[:parsed_limit]
    has_more = len(records) > parsed_limit
    return InvoiceList(
        items=[_invoice_list_item(record) for record in page_records],
        page=InvoicePage(
            limit=parsed_limit,
            next_cursor=(
                _invoice_next_cursor(page_records[-1])
                if has_more and page_records
                else None
            ),
        ),
    )


async def get_invoice_detail(
    *,
    requester: RequestContext,
    invoice_id: str,
    invoices: InvoiceRepository,
) -> InvoiceDetail:
    """현재 회원의 단일 인보이스 상세를 조회합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        invoice_id: 조회할 인보이스 ID입니다.
        invoices: 회원 인보이스를 조회하는 저장소입니다.

    Returns:
        인보이스 상세, 결제 실패, 재시도, 화면 액션 정보를 담은 결과입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
        ResourceNotFoundError: 인보이스가 없거나 현재 회원 소유가 아닌 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")

    record = await invoices.get_invoice_detail_for_user(
        invoice_id,
        requester.user_id,
    )
    if record is None:
        owner_id = await invoices.get_invoice_owner(invoice_id)
        if owner_id is None:
            raise ResourceNotFoundError("invoice was not found")
        if owner_id != requester.user_id:
            raise ForbiddenError("invoice belongs to another user")
        raise ResourceNotFoundError("invoice was not found")
    return _invoice_detail(record)


def _parse_limit(value: int | str) -> int:
    if isinstance(value, int):
        limit = value
    else:
        try:
            limit = int(value)
        except ValueError as exc:
            raise BadRequestError("limit is invalid") from exc
    if limit < 1 or limit > 50:
        raise BadRequestError("limit is invalid")
    return limit


def _parse_invoice_status(value: InvoiceStatus | str | None) -> InvoiceStatus | None:
    if value is None:
        return None
    if value in {"issued", "paid", "voided", "refunded"}:
        return cast(InvoiceStatus, value)
    raise BadRequestError("status is invalid")


def _parse_payment_status(value: PaymentStatus | str | None) -> PaymentStatus | None:
    if value is None:
        return None
    if value == "failed":
        return "failed"
    raise BadRequestError("paymentStatus is invalid")


def _parse_query_date(value: date | str | None, name: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if not _QUERY_DATE_PATTERN.fullmatch(value):
        raise BadRequestError(f"{name} is invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequestError(f"{name} is invalid") from exc


def _invoice_list_item(record: InvoiceListRecord) -> InvoiceListItem:
    is_failed_payment = record.payment_status == "failed"
    return InvoiceListItem(
        invoice_id=record.invoice_id,
        subscription_id=record.subscription_id,
        product_name=record.product_name,
        plan_name=record.plan_name,
        invoice_type=record.invoice_type,
        status=record.status,
        payment_status=record.payment_status if is_failed_payment else None,
        amount=record.amount,
        currency=record.currency,
        billing_date=record.billing_date,
        paid_at=record.paid_at if record.status == "paid" else None,
        receipt_available=record.status == "paid" and record.receipt_available,
        failure_summary=record.failure_summary if is_failed_payment else None,
        detail_url=f"/invoices/{record.invoice_id}",
    )


def _invoice_next_cursor(record: InvoiceListRecord) -> str:
    return encode_cursor(
        {
            "billingDate": record.billing_date,
            "invoiceId": record.invoice_id,
        }
    )


def _invoice_detail(record: InvoiceDetailRecord) -> InvoiceDetail:
    return InvoiceDetail(
        invoice_id=record.invoice_id,
        subscription_id=record.subscription_id,
        subscription_status=record.subscription_status,
        status=record.status,
        payment_status=record.payment_status,
        amount=record.amount,
        currency=record.currency,
        billing_date=record.billing_date,
        paid_at=record.paid_at if record.status == "paid" else None,
        receipt_url=record.receipt_url if record.status == "paid" else None,
        failure=(
            InvoiceFailure(
                code=record.failure_code,
                message=record.failure_message,
                retryable=record.failure_retryable,
            )
            if record.failure_code is not None
            and record.failure_message is not None
            else None
        ),
        retry=InvoiceRetry(
            available=record.retry_available,
            scheduled_at=record.retry_scheduled_at,
        ),
        actions=InvoiceActions(
            billing_method_update_url=_billing_method_update_url(record),
            subscription_manage_url="https://example.com/account/subscription",
        ),
    )


def _billing_method_update_url(record: InvoiceDetailRecord) -> str | None:
    if record.payment_status != "failed":
        return None
    if record.failure_code is None and record.failure_reason is None:
        return None
    if _requires_billing_method_update(record):
        return "https://example.com/account/billing-methods"
    return None


def _requires_billing_method_update(record: InvoiceDetailRecord) -> bool:
    failure_code = (record.failure_code or "").upper()
    failure_reason = (record.failure_reason or "").lower()
    billing_method_failure_codes = {
        "BILLING_RETRY_FAILED",
        "CARD_DECLINED",
        "EXPIRED_CARD",
        "INSUFFICIENT_FUNDS",
        "INVALID_BILLING_KEY",
        "PROVIDER_BILLING_RETRY_FAILED",
    }
    billing_method_failure_reasons = {
        "auth_failed",
        "billing_key_invalid",
        "card_declined",
        "insufficient_funds",
        "provider_rejected",
    }
    return (
        failure_code in billing_method_failure_codes
        or failure_reason in billing_method_failure_reasons
    )
