from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from payments.application.admin_catalog import AdminRequestContext
from payments.application.errors import BadRequestError
from payments.application.operator_audits import (
    get_operator_audit_detail,
    list_operator_audits,
)
from payments.application.ports.operator_audits import OperatorAuditQuery
from payments.http.dependencies import HttpDependencies, admin_context_dependency
from payments.http.schemas.operator_audits import (
    OperatorAuditDetailResponse,
    OperatorAuditListResponse,
    operator_audit_detail_response,
    operator_audit_list_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin-operator-audits"])
    require_audit_read_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("audit_read",),
    )

    @router.get("/operator-audits", response_model=OperatorAuditListResponse)
    async def list_audits(
        context: AdminRequestContext = Depends(require_audit_read_context),
        operatorId: str | None = None,
        action: str | None = None,
        targetType: str | None = None,
        targetId: str | None = None,
        result: Annotated[list[str] | None, Query()] = None,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        cursor: str | None = None,
        limit: str = "50",
    ) -> OperatorAuditListResponse:
        _ = context
        response = await list_operator_audits(
            OperatorAuditQuery(
                operator_id=operatorId,
                action=action,
                target_type=targetType,
                target_id=targetId,
                result=tuple(result) if result is not None else None,
                from_at=_query_datetime(from_, "from"),
                to_at=_query_datetime(to, "to"),
                cursor=cursor,
                limit=_query_limit(limit),
            ),
            dependencies.operator_audits,
        )
        return operator_audit_list_response(response)

    @router.get(
        "/operator-audits/{auditId}",
        response_model=OperatorAuditDetailResponse,
        response_model_exclude_unset=True,
    )
    async def get_audit(
        auditId: str,
        context: AdminRequestContext = Depends(require_audit_read_context),
    ) -> OperatorAuditDetailResponse:
        _ = context
        audit = await get_operator_audit_detail(auditId, dependencies.operator_audits)
        return operator_audit_detail_response(audit)

    return router


def _query_datetime(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequestError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BadRequestError(f"{field_name} is invalid")
    return parsed


def _query_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise BadRequestError("limit is invalid") from exc
    if limit < 1 or limit > 100:
        raise BadRequestError("limit is invalid")
    return limit
