from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from payments.application.errors import (
    AccountLockedError,
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    ConflictResponseError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentApplicationError,
    PaymentRequiredResponseError,
    ProviderError,
    RateLimitError,
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

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            status.HTTP_400_BAD_REQUEST,
            "bad_request",
            str(exc),
        )


def map_application_error(exc: PaymentApplicationError) -> JSONResponse:
    if isinstance(exc, AuthenticationError):
        return error_response(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            str(exc),
        )
    if isinstance(exc, AccountLockedError):
        return error_response(
            status.HTTP_423_LOCKED,
            "account_locked",
            str(exc),
        )
    if isinstance(exc, AuthorizationError):
        return error_response(
            status.HTTP_400_BAD_REQUEST,
            "missing_or_invalid_request_context",
            str(exc),
        )
    if isinstance(exc, BadRequestError):
        return error_response(
            status.HTTP_400_BAD_REQUEST,
            "bad_request",
            str(exc),
        )
    if isinstance(exc, ForbiddenError):
        return error_response(
            status.HTTP_403_FORBIDDEN,
            "forbidden",
            str(exc),
        )
    if isinstance(exc, ResourceNotFoundError):
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            str(exc),
        )
    if isinstance(exc, PaymentRequiredResponseError):
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content=jsonable_encoder(exc.response_body),
        )
    if isinstance(exc, ProviderError):
        return error_response(
            status.HTTP_502_BAD_GATEWAY,
            "provider_error",
            str(exc),
        )
    if isinstance(exc, RateLimitError):
        return error_response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            str(exc),
        )
    if isinstance(exc, IdempotencyConflictError):
        return error_response(
            status.HTTP_409_CONFLICT,
            "idempotency_conflict",
            str(exc),
        )
    if isinstance(exc, ConflictResponseError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=jsonable_encoder(exc.response_body),
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
