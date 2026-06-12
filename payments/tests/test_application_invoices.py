from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from payments.application.context import RequestContext
from payments.application.cursors import decode_cursor
from payments.application.errors import (
    AuthorizationError,
    BadRequestError,
    ForbiddenError,
    ResourceNotFoundError,
)
from payments.application.invoices import (
    InvoiceDetailRecord,
    InvoiceListRecord,
    get_invoice_detail,
    list_user_invoices,
)
from payments.application.ports.invoices import (
    InvoiceStatus,
    PaymentStatus,
    SubscriptionStatus,
)


class FakeInvoiceRepository:
    def __init__(self) -> None:
        self.records: dict[str, list[InvoiceListRecord]] = {}
        self.details: dict[tuple[str, str], InvoiceDetailRecord] = {}
        self.owners: dict[str, str] = {}

    async def list_invoices_for_user(
        self,
        user_id: str,
        limit: int,
        status=None,
        payment_status=None,
        subscription_id: str | None = None,
        from_date=None,
        to_date=None,
        cursor: str | None = None,
    ) -> list[InvoiceListRecord]:
        _ = cursor
        records = self.records.get(user_id, [])
        if status is not None:
            records = [record for record in records if record.status == status]
        if payment_status is not None:
            records = [
                record
                for record in records
                if record.payment_status == payment_status
            ]
        if subscription_id is not None:
            records = [
                record
                for record in records
                if record.subscription_id == subscription_id
            ]
        if from_date is not None:
            records = [
                record for record in records if record.billing_date >= from_date
            ]
        if to_date is not None:
            records = [
                record for record in records if record.billing_date <= to_date
            ]
        records = sorted(
            records,
            key=lambda record: (record.billing_date, record.invoice_id),
            reverse=True,
        )
        if cursor is not None:
            payload = decode_cursor(cursor)
            billing_date = date.fromisoformat(str(payload["billingDate"]))
            invoice_id = str(payload["invoiceId"])
            records = [
                record
                for record in records
                if record.billing_date < billing_date
                or (
                    record.billing_date == billing_date
                    and record.invoice_id < invoice_id
                )
            ]
        return records[:limit]

    async def get_invoice_detail_for_user(
        self,
        invoice_id: str,
        user_id: str,
    ) -> InvoiceDetailRecord | None:
        return self.details.get((user_id, invoice_id))

    async def get_invoice_owner(self, invoice_id: str) -> str | None:
        for user_id, records in self.records.items():
            if any(record.invoice_id == invoice_id for record in records):
                return user_id
        for user_id, invoice_id_key in self.details:
            if invoice_id_key == invoice_id:
                return user_id
        return self.owners.get(invoice_id)


def invoice_list_record(
    invoice_id: str,
    billing_date: date,
    *,
    status: InvoiceStatus = "paid",
    payment_status: PaymentStatus | None = "paid",
    receipt_available: bool = True,
    failure_summary: str | None = None,
) -> InvoiceListRecord:
    return InvoiceListRecord(
        invoice_id=invoice_id,
        subscription_id="sub_123",
        product_name="Analytics Pro",
        plan_name="월간 Pro",
        invoice_type="recurring",
        status=status,
        payment_status=payment_status,
        amount=9900,
        currency="KRW",
        billing_date=billing_date,
        paid_at=datetime(2026, 7, 8, 0, 1, 12, tzinfo=UTC),
        receipt_available=receipt_available,
        failure_summary=failure_summary,
    )


def invoice_detail_record(
    invoice_id: str,
    *,
    subscription_status: SubscriptionStatus | None = "canceled",
    status: InvoiceStatus = "issued",
    payment_status: PaymentStatus | None = "failed",
    paid_at: datetime | None = None,
    receipt_url: str | None = None,
    failure_code: str | None = "INSUFFICIENT_FUNDS",
    failure_reason: str | None = "provider_rejected",
    failure_message: str | None = "잔액 부족으로 결제가 실패했습니다.",
    failure_retryable: bool = True,
    retry_available: bool = True,
    retry_scheduled_at: datetime | None = datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
) -> InvoiceDetailRecord:
    return InvoiceDetailRecord(
        invoice_id=invoice_id,
        subscription_id="sub_123",
        subscription_status=subscription_status,
        status=status,
        payment_status=payment_status,
        amount=9900,
        currency="KRW",
        billing_date=date(2026, 7, 8),
        paid_at=paid_at,
        receipt_url=receipt_url,
        failure_code=failure_code,
        failure_reason=failure_reason,
        failure_message=failure_message,
        failure_retryable=failure_retryable,
        retry_available=retry_available,
        retry_scheduled_at=retry_scheduled_at,
    )


async def test_list_user_invoices_requires_user() -> None:
    with pytest.raises(AuthorizationError):
        await list_user_invoices(
            requester=RequestContext(request_id="req_1"),
            invoices=FakeInvoiceRepository(),
            limit=20,
        )


async def test_list_user_invoices_returns_items_with_detail_urls() -> None:
    repository = FakeInvoiceRepository()
    repository.records["user_1"] = [
        invoice_list_record("inv_202607_123", date(2026, 7, 8)),
        invoice_list_record("inv_202606_123", date(2026, 6, 8)),
    ]

    result = await list_user_invoices(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        invoices=repository,
        limit=20,
    )

    assert result.page.limit == 20
    assert result.page.next_cursor is None
    assert [item.invoice_id for item in result.items] == [
        "inv_202607_123",
        "inv_202606_123",
    ]
    assert result.items[0].detail_url == "/invoices/inv_202607_123"


async def test_list_user_invoices_normalizes_list_display_fields() -> None:
    repository = FakeInvoiceRepository()
    repository.records["user_1"] = [
        invoice_list_record(
            "inv_paid",
            date(2026, 7, 8),
            status="paid",
            payment_status="paid",
            receipt_available=True,
        ),
        invoice_list_record(
            "inv_failed",
            date(2026, 6, 8),
            status="issued",
            payment_status="failed",
            receipt_available=True,
            failure_summary="잔액 부족",
        ),
    ]

    result = await list_user_invoices(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        invoices=repository,
        limit=20,
    )

    assert result.items[0].payment_status is None
    assert result.items[0].receipt_available is True
    assert result.items[0].failure_summary is None
    assert result.items[1].payment_status == "failed"
    assert result.items[1].paid_at is None
    assert result.items[1].receipt_available is False
    assert result.items[1].failure_summary == "잔액 부족"


async def test_list_user_invoices_filters_by_payment_status() -> None:
    repository = FakeInvoiceRepository()
    repository.records["user_1"] = [
        invoice_list_record("inv_paid", date(2026, 7, 8)),
        invoice_list_record(
            "inv_failed",
            date(2026, 6, 8),
            status="issued",
            payment_status="failed",
            receipt_available=False,
            failure_summary="잔액 부족",
        ),
    ]

    result = await list_user_invoices(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        invoices=repository,
        limit=20,
        payment_status="failed",
    )

    assert [item.invoice_id for item in result.items] == ["inv_failed"]
    assert result.items[0].payment_status == "failed"


async def test_list_user_invoices_rejects_invalid_query_params() -> None:
    with pytest.raises(BadRequestError):
        await list_user_invoices(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            invoices=FakeInvoiceRepository(),
            limit="51",
        )

    with pytest.raises(BadRequestError):
        await list_user_invoices(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            invoices=FakeInvoiceRepository(),
            limit="20",
            status="failed",
        )

    with pytest.raises(BadRequestError):
        await list_user_invoices(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            invoices=FakeInvoiceRepository(),
            limit="20",
            payment_status="paid",
        )

    with pytest.raises(BadRequestError):
        await list_user_invoices(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            invoices=FakeInvoiceRepository(),
            limit="20",
            from_date="2026-07-31",
            to_date="2026-07-01",
        )

    for invalid_date in ("20260701", "2026-W27-3"):
        with pytest.raises(BadRequestError):
            await list_user_invoices(
                requester=RequestContext(request_id="req_1", user_id="user_1"),
                invoices=FakeInvoiceRepository(),
                limit="20",
                from_date=invalid_date,
            )


async def test_get_invoice_detail_requires_user() -> None:
    with pytest.raises(AuthorizationError):
        await get_invoice_detail(
            requester=RequestContext(request_id="req_1"),
            invoice_id="inv_123",
            invoices=FakeInvoiceRepository(),
        )


async def test_get_invoice_detail_raises_for_missing_or_other_user() -> None:
    with pytest.raises(ResourceNotFoundError):
        await get_invoice_detail(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            invoice_id="inv_missing",
            invoices=FakeInvoiceRepository(),
        )


async def test_get_invoice_detail_forbids_other_user_invoice() -> None:
    repository = FakeInvoiceRepository()
    repository.owners["inv_123"] = "user_2"

    with pytest.raises(ForbiddenError):
        await get_invoice_detail(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            invoice_id="inv_123",
            invoices=repository,
        )


async def test_get_invoice_detail_returns_failure_and_actions() -> None:
    repository = FakeInvoiceRepository()
    repository.details[("user_1", "inv_123")] = invoice_detail_record(
        "inv_123",
        paid_at=datetime(2026, 7, 8, 0, 1, 12, tzinfo=UTC),
        receipt_url="https://pay.example.com/failed-receipt",
    )

    detail = await get_invoice_detail(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        invoice_id="inv_123",
        invoices=repository,
    )

    assert detail.invoice_id == "inv_123"
    assert detail.subscription_status == "canceled"
    assert detail.paid_at is None
    assert detail.receipt_url is None
    assert detail.failure is not None
    assert detail.failure.code == "INSUFFICIENT_FUNDS"
    assert detail.retry is not None
    assert detail.retry.available is True
    assert detail.actions.billing_method_update_url is not None
    assert detail.actions.billing_method_update_url.endswith("/billing-methods")


async def test_get_invoice_detail_omits_method_action_for_other_failure() -> None:
    repository = FakeInvoiceRepository()
    repository.details[("user_1", "inv_timeout")] = invoice_detail_record(
        "inv_timeout",
        failure_code="PROVIDER_TIMEOUT",
        failure_reason="provider_error",
        failure_message="일시적인 결제 제공자 오류입니다.",
        failure_retryable=True,
    )

    detail = await get_invoice_detail(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        invoice_id="inv_timeout",
        invoices=repository,
    )

    assert detail.failure is not None
    assert detail.failure.code == "PROVIDER_TIMEOUT"
    assert detail.actions.billing_method_update_url is None


async def test_get_invoice_detail_omits_failure_actions_for_paid_invoice() -> None:
    repository = FakeInvoiceRepository()
    paid_at = datetime(2026, 7, 8, 0, 1, 12, tzinfo=UTC)
    repository.details[("user_1", "inv_paid")] = invoice_detail_record(
        "inv_paid",
        status="paid",
        payment_status="paid",
        paid_at=paid_at,
        receipt_url="https://pay.example.com/receipt",
        failure_code=None,
        failure_reason=None,
        failure_message=None,
        failure_retryable=False,
        retry_available=False,
        retry_scheduled_at=None,
    )

    detail = await get_invoice_detail(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        invoice_id="inv_paid",
        invoices=repository,
    )

    assert detail.status == "paid"
    assert detail.paid_at == paid_at
    assert detail.receipt_url == "https://pay.example.com/receipt"
    assert detail.failure is None
    assert detail.retry.available is False
    assert detail.actions.billing_method_update_url is None
