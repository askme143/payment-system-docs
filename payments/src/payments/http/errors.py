from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from payments.application.errors import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentApplicationError,
    ResourceNotFoundError,
)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PaymentApplicationError)
    async def handle_payment_error(
        _: Request, exc: PaymentApplicationError
    ) -> JSONResponse:
        return map_application_error(exc)


def map_application_error(exc: PaymentApplicationError) -> JSONResponse:
    if isinstance(exc, AuthenticationError):
        return error_response(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            str(exc),
        )
    if isinstance(exc, AuthorizationError):
        return error_response(
            status.HTTP_400_BAD_REQUEST,
            "missing_or_invalid_request_context",
            str(exc),
        )
    if isinstance(exc, ResourceNotFoundError):
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            str(exc),
        )
    if isinstance(exc, IdempotencyConflictError):
        return error_response(
            status.HTTP_409_CONFLICT,
            "idempotency_conflict",
            str(exc),
        )
    if isinstance(exc, InvalidStateTransitionError):
        return error_response(
            status.HTTP_409_CONFLICT,
            "invalid_state",
            str(exc),
        )
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "payment_application_error",
        str(exc),
    )
