from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    BadRequestError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentRequiredResponseError,
    ProviderError,
)
from payments.application.ports.billing_auth import BillingAuthRepository
from payments.application.ports.billing_keys import BillingKeyCipher
from payments.application.ports.clock import Clock
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.payment_customers import PaymentCustomerRepository
from payments.application.ports.provider import PaymentProvider
from payments.application.ports.unit_of_work import BillingAuthIssueUnitOfWorkFactory
from payments.domain.entities.billing_auth import BillingAuth
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument

BILLING_AUTH_START_IDEMPOTENCY_SCOPE = "billing-auth"
BILLING_ISSUE_IDEMPOTENCY_SCOPE = "billing-issue"


@dataclass(frozen=True, slots=True)
class BillingAuthStartCommand:
    success_url: str
    fail_url: str
    set_as_default: bool = False


@dataclass(frozen=True, slots=True)
class BillingAuthStartResult:
    billing_auth_id: str
    customer_key: str
    client_key: str
    success_url: str
    fail_url: str
    set_as_default: bool
    status: str


@dataclass(frozen=True, slots=True)
class BillingAuthIssueCommand:
    billing_auth_id: str
    auth_key: str
    customer_key: str


@dataclass(frozen=True, slots=True)
class BillingAuthIssueResult:
    billing_method_id: str
    status: str
    is_default: bool
    method: str
    card_company: str
    masked_card_number: str
    billing_key_status: str
    created_at: datetime


async def start_billing_auth(
    requester: RequestContext,
    command: BillingAuthStartCommand,
    repository: BillingAuthRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    clock: Clock,
    client_key: str,
    idempotency_key: str | None = None,
    allowed_redirect_hosts: tuple[str, ...] = ("example.com",),
) -> BillingAuthStartResult:
    """토스 빌링 인증 시작 데이터를 생성합니다.

    Args:
        requester: 내부 백엔드가 전달한 요청 추적 및 회원 컨텍스트입니다.
        command: 성공/실패 리다이렉트 URL과 기본 결제수단 예약 여부입니다.
        repository: 빌링 인증 시도와 고객 키를 저장하는 저장소입니다.
        clock: 생성/만료 시각을 제공하는 시간 포트입니다.
        client_key: 프론트가 Toss SDK에 전달할 클라이언트 키입니다.

    Returns:
        빌링 인증 시작에 필요한 프론트 입력값입니다.

    Raises:
        AuthorizationError: 회원 컨텍스트 없이 호출된 경우 발생합니다.
    """
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    _validate_redirect_url(command.success_url, allowed_redirect_hosts)
    _validate_redirect_url(command.fail_url, allowed_redirect_hosts)
    payload = {
        "userId": requester.user_id,
        "successUrl": command.success_url,
        "failUrl": command.fail_url,
        "setAsDefault": command.set_as_default,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None
    now = clock.utc_now()
    if key_hash is not None:
        existing_key = await idempotency_keys.find_idempotency_key(
            BILLING_AUTH_START_IDEMPOTENCY_SCOPE,
            key_hash,
        )
        if existing_key is not None and existing_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing_key is not None and existing_key.response_body is not None:
            return _start_result_from_response_body(existing_key.response_body)

    payment_customer = await payment_customers.get_active_payment_customer_for_user(
        requester.user_id
    )
    if payment_customer is None:
        payment_customer = PaymentCustomer(
            id=PaymentCustomer.generate_id(),
            user_id=requester.user_id,
            provider="tosspayments",
            customer_key=PaymentCustomer.generate_pcus_key(),
            status="active",
        )
        await payment_customers.save_payment_customer(payment_customer)

    active_count = await repository.count_active_billing_methods_for_user(
        requester.user_id
    )
    set_as_default = command.set_as_default or active_count == 0
    billing_auth = BillingAuth(
        id=BillingAuth.generate_id(),
        user_id=requester.user_id,
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=set_as_default,
        status="ready",
        success_url=command.success_url,
        fail_url=command.fail_url,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    await repository.save_billing_auth(billing_auth)

    result = BillingAuthStartResult(
        billing_auth_id=billing_auth.id,
        customer_key=payment_customer.customer_key,
        client_key=client_key,
        success_url=_append_billing_auth_id(command.success_url, billing_auth.id),
        fail_url=_append_billing_auth_id(command.fail_url, billing_auth.id),
        set_as_default=set_as_default,
        status=billing_auth.status,
    )
    if key_hash is not None:
        await idempotency_keys.save_idempotency_key(
            IdempotencyKey(
                id=IdempotencyKey.generate_id(),
                scope=BILLING_AUTH_START_IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="succeeded",
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=24),
                resource_type="billing_auth",
                resource_id=billing_auth.id,
                response_status=201,
                response_body=_start_result_to_response_body(result),
            )
        )
    return result


async def issue_billing_key(
    requester: RequestContext,
    command: BillingAuthIssueCommand,
    repository: BillingAuthRepository,
    payment_customers: PaymentCustomerRepository,
    idempotency_keys: IdempotencyKeyRepository,
    provider: PaymentProvider,
    clock: Clock,
    billing_key_cipher: BillingKeyCipher,
    idempotency_key: str,
    billing_auth_issue_uow_factory: BillingAuthIssueUnitOfWorkFactory | None = None,
) -> BillingAuthIssueResult:
    """토스 빌링 인증 성공 값을 빌링키와 결제수단으로 확정합니다."""
    if requester.user_id is None:
        raise AuthorizationError("X-Request-User-Id header is required")
    payload = {
        "billingAuthId": command.billing_auth_id,
        "authKey": command.auth_key,
        "customerKey": command.customer_key,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key)
    now = clock.utc_now()
    billing_auth = await repository.get_billing_auth_for_user(
        command.billing_auth_id,
        requester.user_id,
    )
    if billing_auth is None:
        raise BadRequestError("billingAuthId does not match user")
    existing_key = await idempotency_keys.find_idempotency_key(
        BILLING_ISSUE_IDEMPOTENCY_SCOPE,
        key_hash,
    )
    if existing_key is not None and existing_key.request_hash != request_hash:
        raise IdempotencyConflictError("idempotency key was used with another payload")
    if existing_key is not None and existing_key.response_body is not None:
        if existing_key.response_status == 402:
            raise PaymentRequiredResponseError(
                "billing key issue failed",
                existing_key.response_body,
            )
        return _issue_result_from_response_body(existing_key.response_body)
    if existing_key is not None and existing_key.status == "processing":
        raise InvalidStateTransitionError("billing issue is processing")
    existing_resource_key = await idempotency_keys.find_idempotency_key_by_resource(
        BILLING_ISSUE_IDEMPOTENCY_SCOPE,
        "billing_auth",
        billing_auth.id,
    )
    if existing_resource_key is not None:
        if existing_resource_key.request_hash != request_hash:
            raise IdempotencyConflictError(
                "billing auth was issued with another payload"
            )
        if existing_resource_key.response_body is not None:
            if existing_resource_key.response_status == 402:
                raise PaymentRequiredResponseError(
                    "billing key issue failed",
                    existing_resource_key.response_body,
                )
            return _issue_result_from_response_body(
                existing_resource_key.response_body
            )
        if existing_resource_key.status == "processing":
            raise InvalidStateTransitionError("billing issue is processing")
    if billing_auth.status != "ready":
        raise InvalidStateTransitionError("billing auth cannot be issued")
    if billing_auth.expires_at <= now:
        billing_auth.status = "expired"
        await repository.save_billing_auth(billing_auth)
        raise InvalidStateTransitionError("billing auth expired")
    if command.customer_key != billing_auth.customer_key_snapshot:
        raise BadRequestError("customerKey does not match billing auth")
    payment_customer = await payment_customers.get_active_payment_customer_for_user(
        requester.user_id
    )
    if (
        payment_customer is None
        or payment_customer.id != billing_auth.payment_customer_id
        or payment_customer.customer_key != command.customer_key
    ):
        raise BadRequestError("customerKey does not match payment customer")

    processing_key = IdempotencyKey(
        id=(
            existing_key.id
            if existing_key is not None
            else IdempotencyKey.generate_id()
        ),
        scope=BILLING_ISSUE_IDEMPOTENCY_SCOPE,
        key_hash=key_hash,
        request_hash=request_hash,
        status="processing",
        created_at=existing_key.created_at if existing_key is not None else now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
        resource_type="billing_auth",
        resource_id=billing_auth.id,
        locked_until_at=now + timedelta(minutes=5),
    )
    await idempotency_keys.save_idempotency_key(processing_key)

    try:
        provider_result = await provider.issue_billing_key(
            auth_key=command.auth_key,
            customer_key=command.customer_key,
        )
    except ProviderError as exc:
        failure_body = await _mark_billing_issue_failed(
            billing_auth=billing_auth,
            repository=repository,
            idempotency_keys=idempotency_keys,
            billing_auth_issue_uow_factory=billing_auth_issue_uow_factory,
            processing_key=processing_key,
            request_hash=request_hash,
            now=clock.utc_now(),
            error=exc,
        )
        raise PaymentRequiredResponseError(
            "billing key issue failed",
            failure_body,
        ) from exc
    instrument = PaymentInstrument(
        id=PaymentInstrument.generate_id(),
        payment_customer_id=billing_auth.payment_customer_id,
        provider="tosspayments",
        billing_key=billing_key_cipher.encrypt(provider_result.billing_key),
        billing_key_hash=sha256(provider_result.billing_key.encode()).hexdigest(),
        status="active",
        provider_raw=provider_result.response_summary,
    )
    is_default = billing_auth.set_as_default or (
        await repository.count_active_billing_methods_for_user(requester.user_id) == 0
    )
    billing_method = BillingMethod(
        id=BillingMethod.generate_id(),
        user_id=requester.user_id,
        payment_customer_id=billing_auth.payment_customer_id,
        instrument_id=instrument.id,
        display_name=(
            f"{provider_result.card_company} {provider_result.masked_card_number}"
        ),
        provider="tosspayments",
        is_default=is_default,
        status="active",
        method=provider_result.method,
        card_company=provider_result.card_company,
        billing_key_status="active",
        created_at=now,
        masked_number=provider_result.masked_card_number,
    )
    billing_auth.status = "issued"

    result = BillingAuthIssueResult(
        billing_method_id=billing_method.id,
        status=billing_method.status,
        is_default=billing_method.is_default,
        method=billing_method.method,
        card_company=billing_method.card_company,
        masked_card_number=billing_method.masked_number or "",
        billing_key_status=billing_method.billing_key_status,
        created_at=now,
    )
    await _save_billing_issue_success(
        billing_auth=billing_auth,
        instrument=instrument,
        billing_method=billing_method,
        repository=repository,
        idempotency_keys=idempotency_keys,
        billing_auth_issue_uow_factory=billing_auth_issue_uow_factory,
        processing_key=processing_key,
        request_hash=request_hash,
        updated_at=clock.utc_now(),
        response_body=_issue_result_to_response_body(result),
    )
    return result


def _append_billing_auth_id(url: str, billing_auth_id: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["billingAuthId"] = billing_auth_id
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _validate_redirect_url(url: str, allowed_hosts: tuple[str, ...]) -> None:
    parts = urlsplit(url)
    hostname = parts.hostname.lower() if parts.hostname is not None else None
    if parts.scheme != "https" or hostname is None:
        raise BadRequestError("redirect URL is invalid")
    if not any(_host_matches(hostname, allowed_host) for allowed_host in allowed_hosts):
        raise BadRequestError("redirect URL host is not allowed")


def _host_matches(hostname: str, allowed_host: str) -> bool:
    normalized_allowed_host = allowed_host.strip().lower().rstrip(".")
    normalized_hostname = hostname.rstrip(".")
    return (
        normalized_hostname == normalized_allowed_host
        or normalized_hostname.endswith(f".{normalized_allowed_host}")
    )


def _start_result_to_response_body(result: BillingAuthStartResult) -> dict[str, object]:
    return {
        "billingAuthId": result.billing_auth_id,
        "customerKey": result.customer_key,
        "clientKey": result.client_key,
        "successUrl": result.success_url,
        "failUrl": result.fail_url,
        "setAsDefault": result.set_as_default,
        "status": result.status,
    }


def _start_result_from_response_body(
    body: dict[str, object],
) -> BillingAuthStartResult:
    set_as_default = body["setAsDefault"]
    if not isinstance(set_as_default, bool):
        raise InvalidStateTransitionError(
            "idempotency response setAsDefault is invalid"
        )
    return BillingAuthStartResult(
        billing_auth_id=str(body["billingAuthId"]),
        customer_key=str(body["customerKey"]),
        client_key=str(body["clientKey"]),
        success_url=str(body["successUrl"]),
        fail_url=str(body["failUrl"]),
        set_as_default=set_as_default,
        status=str(body["status"]),
    )


def _issue_result_to_response_body(result: BillingAuthIssueResult) -> dict[str, object]:
    return {
        "billingMethodId": result.billing_method_id,
        "status": result.status,
        "isDefault": result.is_default,
        "method": result.method,
        "cardCompany": result.card_company,
        "maskedCardNumber": result.masked_card_number,
        "billingKeyStatus": result.billing_key_status,
        "createdAt": result.created_at,
    }


async def _mark_billing_issue_failed(
    *,
    billing_auth: BillingAuth,
    repository: BillingAuthRepository,
    idempotency_keys: IdempotencyKeyRepository,
    billing_auth_issue_uow_factory: BillingAuthIssueUnitOfWorkFactory | None,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    error: ProviderError,
) -> dict[str, object]:
    billing_auth.status = "failed"
    billing_auth.failure = _billing_key_issue_failure(error)
    response_body = {
        "billingAuthId": billing_auth.id,
        "status": billing_auth.status,
        "failure": billing_auth.failure,
    }
    if billing_auth_issue_uow_factory is not None:
        async with billing_auth_issue_uow_factory() as uow:
            await _save_billing_issue_failure_records(
                billing_auth=billing_auth,
                repository=uow.billing_auths,
                idempotency_keys=uow.idempotency_keys,
                processing_key=processing_key,
                request_hash=request_hash,
                now=now,
                response_body=response_body,
            )
        return response_body
    await _save_billing_issue_failure_records(
        billing_auth=billing_auth,
        repository=repository,
        idempotency_keys=idempotency_keys,
        processing_key=processing_key,
        request_hash=request_hash,
        now=now,
        response_body=response_body,
    )
    return response_body


async def _save_billing_issue_failure_records(
    *,
    billing_auth: BillingAuth,
    repository: BillingAuthRepository,
    idempotency_keys: IdempotencyKeyRepository,
    processing_key: IdempotencyKey,
    request_hash: str,
    now: datetime,
    response_body: dict[str, object],
) -> None:
    await repository.save_billing_auth(billing_auth)
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=processing_key.id,
            scope=BILLING_ISSUE_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash,
            request_hash=request_hash,
            status="failed",
            created_at=processing_key.created_at,
            updated_at=now,
            expires_at=processing_key.expires_at,
            resource_type="billing_auth",
            resource_id=billing_auth.id,
            response_status=402,
            response_body=response_body,
        )
    )


async def _save_billing_issue_success(
    *,
    billing_auth: BillingAuth,
    instrument: PaymentInstrument,
    billing_method: BillingMethod,
    repository: BillingAuthRepository,
    idempotency_keys: IdempotencyKeyRepository,
    billing_auth_issue_uow_factory: BillingAuthIssueUnitOfWorkFactory | None,
    processing_key: IdempotencyKey,
    request_hash: str,
    updated_at: datetime,
    response_body: dict[str, object],
) -> None:
    if billing_auth_issue_uow_factory is not None:
        async with billing_auth_issue_uow_factory() as uow:
            await _save_billing_issue_success_records(
                billing_auth=billing_auth,
                instrument=instrument,
                billing_method=billing_method,
                repository=uow.billing_auths,
                idempotency_keys=uow.idempotency_keys,
                processing_key=processing_key,
                request_hash=request_hash,
                updated_at=updated_at,
                response_body=response_body,
            )
        return
    await _save_billing_issue_success_records(
        billing_auth=billing_auth,
        instrument=instrument,
        billing_method=billing_method,
        repository=repository,
        idempotency_keys=idempotency_keys,
        processing_key=processing_key,
        request_hash=request_hash,
        updated_at=updated_at,
        response_body=response_body,
    )


async def _save_billing_issue_success_records(
    *,
    billing_auth: BillingAuth,
    instrument: PaymentInstrument,
    billing_method: BillingMethod,
    repository: BillingAuthRepository,
    idempotency_keys: IdempotencyKeyRepository,
    processing_key: IdempotencyKey,
    request_hash: str,
    updated_at: datetime,
    response_body: dict[str, object],
) -> None:
    if billing_method.is_default:
        await repository.clear_default_billing_methods_for_user(billing_method.user_id)
    await repository.save_payment_instrument(instrument)
    await repository.save_billing_method(billing_method)
    await repository.save_billing_auth(billing_auth)
    await idempotency_keys.save_idempotency_key(
        IdempotencyKey(
            id=processing_key.id,
            scope=BILLING_ISSUE_IDEMPOTENCY_SCOPE,
            key_hash=processing_key.key_hash,
            request_hash=request_hash,
            status="succeeded",
            created_at=processing_key.created_at,
            updated_at=updated_at,
            expires_at=processing_key.expires_at,
            resource_type="billing_auth",
            resource_id=billing_auth.id,
            response_status=201,
            response_body=response_body,
        )
    )


def _billing_key_issue_failure(error: ProviderError) -> dict[str, object]:
    return {
        "code": "BILLING_KEY_ISSUE_FAILED",
        "providerCode": error.provider_code or "PROVIDER_BILLING_KEY_ISSUE_FAILED",
        "message": str(error),
        "retryable": error.retryable,
    }


def _issue_result_from_response_body(
    body: dict[str, object],
) -> BillingAuthIssueResult:
    is_default = body["isDefault"]
    created_at = body["createdAt"]
    if not isinstance(is_default, bool):
        raise InvalidStateTransitionError("idempotency response isDefault is invalid")
    if not isinstance(created_at, datetime):
        raise InvalidStateTransitionError("idempotency response createdAt is invalid")
    return BillingAuthIssueResult(
        billing_method_id=str(body["billingMethodId"]),
        status=str(body["status"]),
        is_default=is_default,
        method=str(body["method"]),
        card_company=str(body["cardCompany"]),
        masked_card_number=str(body["maskedCardNumber"]),
        billing_key_status=str(body["billingKeyStatus"]),
        created_at=created_at,
    )


def _hash_payload(payload: Mapping[str, object]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
