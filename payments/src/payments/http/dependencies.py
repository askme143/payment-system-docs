from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Header

from payments.application.context import RequestContext
from payments.application.errors import AuthenticationError, AuthorizationError
from payments.application.ports import (
    CatalogRepository,
    Clock,
    OneTimePaymentUnitOfWorkFactory,
    PaymentAttemptRepository,
)


@dataclass(frozen=True, slots=True)
class HttpDependencies:
    catalog_repository: CatalogRepository
    one_time_payment_uow_factory: OneTimePaymentUnitOfWorkFactory
    payment_attempts: PaymentAttemptRepository
    clock: Clock
    internal_service_token: str


def request_context_dependency(
    internal_service_token: str,
    require_user: bool,
):
    async def dependency(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
        x_request_user_id: Annotated[
            str | None, Header(alias="X-Request-User-Id")
        ] = None,
    ) -> RequestContext:
        return build_request_context(
            internal_service_token=internal_service_token,
            require_user=require_user,
            authorization=authorization,
            x_request_id=x_request_id,
            x_request_user_id=x_request_user_id,
        )

    return dependency


def build_request_context(
    internal_service_token: str,
    require_user: bool,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    x_request_user_id: Annotated[str | None, Header(alias="X-Request-User-Id")] = None,
) -> RequestContext:
    expected = f"Bearer {internal_service_token}"
    if authorization != expected:
        raise AuthenticationError("valid internal service authorization is required")
    if not x_request_id:
        raise AuthorizationError("X-Request-Id header is required")
    if require_user and not x_request_user_id:
        raise AuthorizationError("X-Request-User-Id header is required")
    return RequestContext(request_id=x_request_id, user_id=x_request_user_id)
