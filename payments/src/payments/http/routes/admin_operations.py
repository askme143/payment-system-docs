from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query

from payments.application.admin_catalog import AdminRequestContext
from payments.application.admin_operations import (
    AdminPaymentCancelCommand,
    AdminSubscriptionAdjustCommand,
    AdminSubscriptionStatus,
    SubscriptionAdjustmentType,
    adjust_admin_subscription,
    cancel_admin_payment,
    list_admin_payments,
    list_admin_subscriptions,
)
from payments.application.errors import BadRequestError
from payments.application.ports import AdminListQuery
from payments.http.dependencies import HttpDependencies, admin_context_dependency
from payments.http.schemas.admin_operations import (
    AdminPaymentCancelRequest,
    AdminPaymentCancelResponse,
    AdminPaymentListResponse,
    AdminSubscriptionAdjustPostponeByRequest,
    AdminSubscriptionAdjustRequest,
    AdminSubscriptionAdjustResponse,
    AdminSubscriptionListResponse,
    admin_payment_cancel_response,
    admin_payment_list_response,
    admin_subscription_adjust_response,
    admin_subscription_list_response,
)


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin-operations"])
    require_payment_read_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("payment_read", "payment_cancel"),
    )
    require_payment_cancel_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("payment_cancel",),
    )
    require_subscription_read_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("subscription_read", "subscription_adjust"),
    )
    require_subscription_adjust_context = admin_context_dependency(
        dependencies.admin_auth,
        dependencies.clock,
        dependencies.internal_service_token,
        ("subscription_adjust",),
    )

    @router.get("/payments", response_model=AdminPaymentListResponse)
    async def admin_payments(
        context: AdminRequestContext = Depends(require_payment_read_context),
        status: Annotated[list[str] | None, Query()] = None,
        userId: str | None = None,
        orderId: str | None = None,
        paymentKey: str | None = None,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        cursor: str | None = None,
        limit: str = "50",
    ) -> AdminPaymentListResponse:
        result = await list_admin_payments(
            AdminListQuery(
                status=tuple(status) if status is not None else None,
                user_id=userId,
                order_id=orderId,
                payment_key=paymentKey,
                from_at=_admin_query_datetime(from_, "from"),
                to_at=_admin_query_datetime(to, "to"),
                cursor=cursor,
                limit=_admin_query_limit(limit),
            ),
            dependencies.admin_operations,
            context,
            dependencies.clock,
        )
        return admin_payment_list_response(result)

    @router.post(
        "/payments/{paymentId}/cancel",
        response_model=AdminPaymentCancelResponse,
    )
    async def cancel_payment(
        paymentId: str,
        request: AdminPaymentCancelRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: AdminRequestContext = Depends(require_payment_cancel_context),
    ) -> AdminPaymentCancelResponse:
        result = await cancel_admin_payment(
            context,
            paymentId,
            AdminPaymentCancelCommand(
                cancel_amount=_admin_cancel_amount(request.cancel_amount),
                cancel_reason=_admin_required_text(
                    request.cancel_reason,
                    "cancelReason",
                ),
                reason_message=_admin_required_text(
                    request.reason_message,
                    "reasonMessage",
                ),
                notify_customer=_admin_body_bool(
                    request.notify_customer,
                    "notifyCustomer",
                ),
            ),
            dependencies.one_time_payment_uow_factory,
            dependencies.payment_provider,
            dependencies.clock,
            idempotency_key=_admin_required_header(
                idempotency_key,
                "Idempotency-Key",
            ),
            operation_locks=dependencies.operation_locks,
            notification_dependencies=dependencies.notification_enqueue,
        )
        return admin_payment_cancel_response(result)

    @router.get("/subscriptions", response_model=AdminSubscriptionListResponse)
    async def admin_subscriptions(
        context: AdminRequestContext = Depends(require_subscription_read_context),
        status: Annotated[list[str] | None, Query()] = None,
        userId: str | None = None,
        productCode: str | None = None,
        paymentFailure: str | None = None,
        nextBillingFrom: str | None = None,
        nextBillingTo: str | None = None,
        cursor: str | None = None,
        limit: str = "50",
    ) -> AdminSubscriptionListResponse:
        result = await list_admin_subscriptions(
            AdminListQuery(
                status=tuple(status) if status is not None else None,
                user_id=userId,
                product_code=productCode,
                payment_failure=_admin_query_bool(
                    paymentFailure,
                    "paymentFailure",
                ),
                next_billing_from=_admin_query_datetime(
                    nextBillingFrom,
                    "nextBillingFrom",
                ),
                next_billing_to=_admin_query_datetime(
                    nextBillingTo,
                    "nextBillingTo",
                ),
                cursor=cursor,
                limit=_admin_query_limit(limit),
            ),
            dependencies.admin_operations,
            context,
            dependencies.clock,
        )
        return admin_subscription_list_response(result)

    @router.post(
        "/subscriptions/{subscriptionId}/adjust",
        response_model=AdminSubscriptionAdjustResponse,
    )
    async def adjust_subscription(
        subscriptionId: str,
        request: AdminSubscriptionAdjustRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: AdminRequestContext = Depends(require_subscription_adjust_context),
    ) -> AdminSubscriptionAdjustResponse:
        result = await adjust_admin_subscription(
            context,
            subscriptionId,
            AdminSubscriptionAdjustCommand(
                adjustment_type=_admin_adjustment_type(request.adjustment_type),
                payment_key=_admin_optional_text(request.payment_key, "paymentKey"),
                invoice_id=_admin_optional_text(request.invoice_id, "invoiceId"),
                postpone_days=_admin_postpone_days(request.postpone_by),
                next_billing_at=_admin_body_datetime(
                    request.next_billing_at,
                    "nextBillingAt",
                ),
                target_status=_admin_subscription_status(request.target_status),
                reason_code=_admin_required_text(request.reason_code, "reasonCode"),
                reason_message=_admin_required_text(
                    request.reason_message,
                    "reasonMessage",
                ),
                notify_customer=_admin_body_bool(
                    request.notify_customer,
                    "notifyCustomer",
                ),
            ),
            dependencies.admin_operations,
            dependencies.idempotency_keys,
            dependencies.clock,
            idempotency_key=_admin_required_header(
                idempotency_key,
                "Idempotency-Key",
            ),
            operation_locks=dependencies.operation_locks,
            provider=dependencies.payment_provider,
            admin_subscription_adjust_uow_factory=(
                dependencies.admin_subscription_adjust_uow_factory
            ),
            notification_dependencies=dependencies.notification_enqueue,
        )
        return admin_subscription_adjust_response(result)

    return router


def _admin_query_datetime(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequestError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BadRequestError(f"{field_name} is invalid")
    return parsed


def _admin_query_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise BadRequestError("limit is invalid") from exc
    if limit < 1 or limit > 100:
        raise BadRequestError("limit is invalid")
    return limit


def _admin_query_bool(value: str | None, field_name: str) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise BadRequestError(f"{field_name} is invalid")


def _admin_required_header(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value


def _admin_required_text(value: object | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is required")
    return value.strip()


def _admin_cancel_amount(value: object | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BadRequestError("cancelAmount is invalid")
    return value


def _admin_body_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise BadRequestError(f"{field_name} is invalid")
    return value


def _admin_optional_text(value: object | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(f"{field_name} is invalid")
    return value.strip()


def _admin_body_datetime(
    value: object | None,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BadRequestError(f"{field_name} is invalid") from exc
    else:
        raise BadRequestError(f"{field_name} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BadRequestError(f"{field_name} is invalid")
    return parsed


def _admin_postpone_days(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, AdminSubscriptionAdjustPostponeByRequest):
        days = value.days
    elif isinstance(value, dict):
        days = value.get("days")
    else:
        raise BadRequestError("postponeBy is invalid")
    if not isinstance(days, int) or isinstance(days, bool):
        raise BadRequestError("postponeBy.days is invalid")
    return days


def _admin_adjustment_type(value: object | None) -> SubscriptionAdjustmentType:
    if value == "provider_payment_sync":
        return "provider_payment_sync"
    if value == "postpone_next_billing":
        return "postpone_next_billing"
    if value == "set_next_billing_date":
        return "set_next_billing_date"
    if value == "clear_payment_failure":
        return "clear_payment_failure"
    if value == "status_override":
        return "status_override"
    raise BadRequestError("adjustmentType is invalid")


def _admin_subscription_status(value: object | None) -> AdminSubscriptionStatus | None:
    if value is None:
        return None
    if value == "active":
        return "active"
    if value == "past_due":
        return "past_due"
    if value == "cancel_scheduled":
        return "cancel_scheduled"
    if value == "canceled":
        return "canceled"
    raise BadRequestError("targetStatus is invalid")
