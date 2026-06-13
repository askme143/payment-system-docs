from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from payments.application.cursors import encode_cursor
from payments.application.errors import BadRequestError, ResourceNotFoundError
from payments.application.ports.operator_audits import (
    OperatorAuditQuery,
    OperatorAuditRepository,
)
from payments.domain.entities.operator_audit import OperatorAudit

_AUDIT_RESULTS = frozenset({"succeeded", "failed", "rejected"})


@dataclass(frozen=True, slots=True)
class OperatorAuditPage:
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class OperatorAuditListItem:
    audit_id: str
    operator_id: str
    action: str
    target_type: str
    target_id: str
    result: str
    reason_code: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperatorAuditListResult:
    items: list[OperatorAuditListItem]
    page: OperatorAuditPage


async def list_operator_audits(
    query: OperatorAuditQuery,
    repository: OperatorAuditRepository,
) -> OperatorAuditListResult:
    """운영자 감사 로그 목록을 요약 형태로 조회합니다."""
    _validate_query(query)
    records = await repository.list_operator_audits(
        replace(query, limit=query.limit + 1)
    )
    page_records = records[: query.limit]
    has_more = len(records) > query.limit
    return OperatorAuditListResult(
        items=[_list_item(record) for record in page_records],
        page=OperatorAuditPage(
            next_cursor=(
                _operator_audit_next_cursor(page_records[-1])
                if has_more and page_records
                else None
            ),
            has_more=has_more,
        ),
    )


async def get_operator_audit_detail(
    audit_id: str,
    repository: OperatorAuditRepository,
) -> OperatorAudit:
    """운영자 감사 로그 상세를 조회합니다."""
    audit = await repository.get_operator_audit(audit_id)
    if audit is None:
        raise ResourceNotFoundError("operator audit not found")
    return audit


def _validate_query(query: OperatorAuditQuery) -> None:
    if query.limit < 1 or query.limit > 100:
        raise BadRequestError("limit is invalid")
    if query.result is not None and set(query.result) - _AUDIT_RESULTS:
        raise BadRequestError("result is invalid")
    if (
        query.from_at is not None
        and query.to_at is not None
        and query.from_at > query.to_at
    ):
        raise BadRequestError("date range is invalid")


def _list_item(audit: OperatorAudit) -> OperatorAuditListItem:
    return OperatorAuditListItem(
        audit_id=audit.id,
        operator_id=audit.operator_id,
        action=audit.action,
        target_type=audit.target_type,
        target_id=audit.target_id,
        result=audit.result,
        reason_code=audit.reason_code,
        created_at=audit.created_at,
    )


def _operator_audit_next_cursor(audit: OperatorAudit) -> str:
    return encode_cursor({"createdAt": audit.created_at, "auditId": audit.id})
