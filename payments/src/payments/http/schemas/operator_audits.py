from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from payments.application.operator_audits import OperatorAuditListResult
from payments.domain.entities.operator_audit import OperatorAudit


class OperatorAuditPageResponse(BaseModel):
    next_cursor: str | None = Field(alias="nextCursor")
    has_more: bool = Field(alias="hasMore")


class OperatorAuditListItemResponse(BaseModel):
    audit_id: str = Field(alias="auditId")
    operator_id: str = Field(alias="operatorId")
    action: str
    target_type: str = Field(alias="targetType")
    target_id: str = Field(alias="targetId")
    result: str
    reason_code: str = Field(alias="reasonCode")
    created_at: datetime = Field(alias="createdAt")


class OperatorAuditListResponse(BaseModel):
    items: list[OperatorAuditListItemResponse]
    page: OperatorAuditPageResponse


class OperatorAuditStateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    run_id: str | None = Field(default=None, alias="runId")
    job_type: str | None = Field(default=None, alias="jobType")


class OperatorAuditDetailResponse(OperatorAuditListItemResponse):
    previous_state: OperatorAuditStateResponse = Field(alias="previousState")
    next_state: OperatorAuditStateResponse = Field(alias="nextState")
    reason_message: str | None = Field(alias="reasonMessage")
    request_ip: str | None = Field(alias="requestIp")
    idempotency_key_id: str | None = Field(alias="idempotencyKeyId")
    idempotency_scope: str | None = Field(alias="idempotencyScope")
    idempotency_key_hash: str | None = Field(alias="idempotencyKeyHash")
    idempotency_request_hash: str | None = Field(alias="idempotencyRequestHash")


def operator_audit_list_response(
    result: OperatorAuditListResult,
) -> OperatorAuditListResponse:
    return OperatorAuditListResponse(
        items=[
            OperatorAuditListItemResponse(
                auditId=item.audit_id,
                operatorId=item.operator_id,
                action=item.action,
                targetType=item.target_type,
                targetId=item.target_id,
                result=item.result,
                reasonCode=item.reason_code,
                createdAt=item.created_at,
            )
            for item in result.items
        ],
        page=OperatorAuditPageResponse(
            nextCursor=result.page.next_cursor,
            hasMore=result.page.has_more,
        ),
    )


def operator_audit_detail_response(
    audit: OperatorAudit,
) -> OperatorAuditDetailResponse:
    return OperatorAuditDetailResponse(
        auditId=audit.id,
        operatorId=audit.operator_id,
        action=audit.action,
        targetType=audit.target_type,
        targetId=audit.target_id,
        result=audit.result,
        reasonCode=audit.reason_code,
        createdAt=audit.created_at,
        previousState=OperatorAuditStateResponse.model_validate(
            audit.previous_state
        ),
        nextState=OperatorAuditStateResponse.model_validate(audit.next_state),
        reasonMessage=audit.reason_message,
        requestIp=audit.request_ip,
        idempotencyKeyId=audit.idempotency_key_id,
        idempotencyScope=audit.idempotency_scope,
        idempotencyKeyHash=audit.idempotency_key_hash,
        idempotencyRequestHash=audit.idempotency_request_hash,
    )
