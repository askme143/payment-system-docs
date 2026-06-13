from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from payments.adapters.crypto import FernetBillingKeyCipher, FernetTemplateArgCipher
from payments.adapters.mongo.admin_auth import MongoAdminAuthRepository
from payments.adapters.mongo.admin_catalog import MongoAdminCatalogRepository
from payments.adapters.mongo.admin_operations import MongoAdminOperationsRepository
from payments.adapters.mongo.billing_auth import MongoBillingAuthRepository
from payments.adapters.mongo.billing_methods import MongoBillingMethodRepository
from payments.adapters.mongo.billing_retry import MongoBillingRetryRepository
from payments.adapters.mongo.catalog import MongoCatalogRepository
from payments.adapters.mongo.idempotency import MongoIdempotencyKeyRepository
from payments.adapters.mongo.invoices import MongoInvoiceRepository
from payments.adapters.mongo.notifications import (
    MongoNotificationOutboxRepository,
    MongoNotificationTemplateRepository,
)
from payments.adapters.mongo.operation_locks import MongoOperationLockRepository
from payments.adapters.mongo.payment_attempts import MongoPaymentAttemptRepository
from payments.adapters.mongo.payment_customers import MongoPaymentCustomerRepository
from payments.adapters.mongo.subscriptions import (
    MongoSubscriptionAccountRepository,
    MongoSubscriptionCheckoutRepository,
    MongoSubscriptionExpirationRepository,
)
from payments.adapters.mongo.unit_of_work import (
    MongoAdminAuthUnitOfWorkFactory,
    MongoAdminSubscriptionAdjustUnitOfWorkFactory,
    MongoBillingAuthIssueUnitOfWorkFactory,
    MongoBillingMethodDefaultUnitOfWorkFactory,
    MongoBillingMethodDeleteUnitOfWorkFactory,
    MongoOneTimePaymentUnitOfWorkFactory,
    MongoSubscriptionBillingUnitOfWorkFactory,
    MongoSubscriptionCancelUnitOfWorkFactory,
    MongoSubscriptionChangeUnitOfWorkFactory,
    MongoSubscriptionConfirmUnitOfWorkFactory,
    MongoSubscriptionExpirationUnitOfWorkFactory,
    MongoSubscriptionResumeUnitOfWorkFactory,
    MongoWebhookUnitOfWorkFactory,
)
from payments.adapters.mongo.webhooks import MongoWebhookRepository
from payments.adapters.notifications import (
    AdminAuthOutboxEmailSender,
    HttpNotificationRecipientResolver,
)
from payments.adapters.rate_limit import InMemoryAdminAuthRateLimiter
from payments.adapters.subscription_change_tokens import (
    HmacSubscriptionChangeTokenCodec,
)
from payments.adapters.time import SystemClock
from payments.adapters.toss import TossPaymentProvider
from payments.application.notifications import NotificationEnqueueDependencies
from payments.http.config import PaymentHttpConfig
from payments.http.dependencies import HttpDependencies
from payments.http.errors import register_error_handlers
from payments.http.router import create_router

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)

_REQUIRED_HEADER_NAMES = {
    "authorization",
    "internal-job-token",
    "toss-signature",
    "x-request-id",
}
_REQUIRED_IDEMPOTENCY_OPERATIONS = {
    ("post", "/payments/confirm"),
    ("post", "/payments/{paymentId}/cancel"),
    ("post", "/subscriptions/confirm"),
    ("patch", "/subscriptions/{subscriptionId}"),
    ("post", "/billing/issue"),
    ("post", "/internal/subscription-billing/{invoiceId}/retry"),
    ("post", "/admin/payments/{paymentId}/cancel"),
    ("post", "/admin/subscriptions/{subscriptionId}/adjust"),
}
_DOCUMENTATION_PATH = Path(__file__).resolve().parents[4] / "docs-data" / (
    "documentation.json"
)


def build_http_dependencies(
    database: AsyncIOMotorDatabase,
    config: PaymentHttpConfig,
) -> HttpDependencies:
    notification_outbox = MongoNotificationOutboxRepository(
        database.notification_outbox
    )
    notification_templates = MongoNotificationTemplateRepository(
        database.notification_templates
    )
    notification_recipient_resolver = HttpNotificationRecipientResolver(
        recipient_api_base_url=config.notification_recipient_api_base_url,
        admin_accounts=database.admin_accounts,
    )
    notification_template_arg_cipher = FernetTemplateArgCipher(
        config.notification_template_arg_encryption_secret
        or config.internal_service_token
    )
    clock = SystemClock()
    return HttpDependencies(
        admin_catalog=MongoAdminCatalogRepository(
            products=database.products,
            operator_audits=database.operator_audits,
            subscription_plans=database.subscription_plans,
            one_time_skus=database.one_time_skus,
        ),
        admin_auth=MongoAdminAuthRepository(
            admin_accounts=database.admin_accounts,
            admin_auth_tokens=database.admin_auth_tokens,
        ),
        admin_auth_uow_factory=MongoAdminAuthUnitOfWorkFactory(database),
        admin_auth_email_sender=AdminAuthOutboxEmailSender(
            link_base_url=config.admin_auth_link_base_url,
            outbox_repository=notification_outbox,
            template_repository=notification_templates,
            recipient_resolver=notification_recipient_resolver,
            template_arg_cipher=notification_template_arg_cipher,
            clock=clock,
        ),
        admin_auth_rate_limiter=InMemoryAdminAuthRateLimiter(),
        admin_operations=MongoAdminOperationsRepository(
            payments=database.payments,
            invoices=database.invoices,
            checkouts=database.checkouts,
            subscriptions=database.subscriptions,
            subscription_plans=database.subscription_plans,
            products=database.products,
            billing_methods=database.billing_methods,
            operator_audits=database.operator_audits,
        ),
        admin_subscription_adjust_uow_factory=(
            MongoAdminSubscriptionAdjustUnitOfWorkFactory(database)
        ),
        billing_auths=MongoBillingAuthRepository(
            billing_auths=database.billing_auths,
            payment_customers=database.payment_customers,
            billing_methods=database.billing_methods,
            payment_instruments=database.payment_instruments,
        ),
        billing_auth_issue_uow_factory=MongoBillingAuthIssueUnitOfWorkFactory(
            database
        ),
        catalog_repository=MongoCatalogRepository(
            database.products,
            database.subscription_plans,
            database.subscriptions,
        ),
        billing_methods=MongoBillingMethodRepository(
            billing_methods=database.billing_methods,
            subscriptions=database.subscriptions,
            payment_instruments=database.payment_instruments,
        ),
        billing_method_default_uow_factory=(
            MongoBillingMethodDefaultUnitOfWorkFactory(database)
        ),
        billing_method_delete_uow_factory=(
            MongoBillingMethodDeleteUnitOfWorkFactory(database)
        ),
        billing_retries=MongoBillingRetryRepository(
            invoices=database.invoices,
            payments=database.payments,
            subscriptions=database.subscriptions,
            subscription_plans=database.subscription_plans,
            billing_methods=database.billing_methods,
            payment_instruments=database.payment_instruments,
        ),
        invoices=MongoInvoiceRepository(
            invoices=database.invoices,
            payments=database.payments,
            subscriptions=database.subscriptions,
            subscription_plans=database.subscription_plans,
            products=database.products,
        ),
        idempotency_keys=MongoIdempotencyKeyRepository(database.idempotency_keys),
        operation_locks=MongoOperationLockRepository(
            operation_locks=database.operation_locks,
            operation_lock_counters=database.operation_lock_counters,
        ),
        one_time_payment_uow_factory=MongoOneTimePaymentUnitOfWorkFactory(database),
        payment_attempts=MongoPaymentAttemptRepository(
            checkouts=database.checkouts,
            payments=database.payments,
        ),
        payment_customers=MongoPaymentCustomerRepository(database.payment_customers),
        payment_provider=TossPaymentProvider(
            secret_key=config.toss_secret_key,
            base_url=config.toss_base_url,
        ),
        subscription_accounts=MongoSubscriptionAccountRepository(
            subscriptions=database.subscriptions,
            subscription_plans=database.subscription_plans,
            products=database.products,
            billing_methods=database.billing_methods,
            payment_instruments=database.payment_instruments,
        ),
        subscription_billing_uow_factory=MongoSubscriptionBillingUnitOfWorkFactory(
            database,
        ),
        subscription_change_tokens=HmacSubscriptionChangeTokenCodec(
            config.internal_service_token,
        ),
        subscription_change_uow_factory=MongoSubscriptionChangeUnitOfWorkFactory(
            database,
        ),
        subscription_checkouts=MongoSubscriptionCheckoutRepository(
            database.subscriptions,
            database.payments,
            database.invoices,
        ),
        subscription_cancel_uow_factory=MongoSubscriptionCancelUnitOfWorkFactory(
            database,
        ),
        subscription_confirm_uow_factory=MongoSubscriptionConfirmUnitOfWorkFactory(
            database,
        ),
        subscription_expirations=MongoSubscriptionExpirationRepository(
            database.subscriptions,
        ),
        subscription_expiration_uow_factory=(
            MongoSubscriptionExpirationUnitOfWorkFactory(database)
        ),
        subscription_resume_uow_factory=MongoSubscriptionResumeUnitOfWorkFactory(
            database,
        ),
        notification_enqueue=NotificationEnqueueDependencies(
            outbox_repository=notification_outbox,
            template_repository=notification_templates,
            recipient_resolver=notification_recipient_resolver,
            template_arg_cipher=notification_template_arg_cipher,
            clock=clock,
        ),
        webhooks=MongoWebhookRepository(
            webhook_events=database.webhook_events,
            payments=database.payments,
            checkouts=database.checkouts,
            one_time_skus=database.one_time_skus,
            invoices=database.invoices,
            subscriptions=database.subscriptions,
        ),
        webhook_uow_factory=MongoWebhookUnitOfWorkFactory(database),
        billing_key_cipher=FernetBillingKeyCipher(
            config.billing_key_encryption_secret or config.internal_service_token
        ),
        clock=clock,
        internal_service_token=config.internal_service_token,
        toss_client_key=config.toss_client_key,
        toss_webhook_secret=config.toss_webhook_secret,
        allowed_redirect_hosts=config.allowed_redirect_hosts,
    )


def create_app(dependencies: HttpDependencies) -> FastAPI:
    app = FastAPI(title="Payment System API")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    register_error_handlers(app)
    app.include_router(create_router(dependencies))
    _install_header_contract_openapi(app)
    return app


def create_mongo_database(config: PaymentHttpConfig) -> AsyncIOMotorDatabase:
    from datetime import UTC

    client = AsyncIOMotorClient(config.database_url, tz_aware=True, tzinfo=UTC)
    return client[config.database_name]


def _install_header_contract_openapi(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, object]:
        if app.openapi_schema:
            return cast(dict[str, object], app.openapi_schema)
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        _mark_required_header_parameters(schema)
        _add_documented_responses(schema)
        _add_documented_response_body_schemas(schema)
        _add_documented_request_body_requirements(schema)
        app.openapi_schema = schema
        return cast(dict[str, object], schema)

    app.openapi = custom_openapi


def _mark_required_header_parameters(schema: dict[str, object]) -> None:
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return
    for raw_path, raw_path_item in paths.items():
        if not isinstance(raw_path, str) or not isinstance(raw_path_item, dict):
            continue
        for raw_method, raw_operation in raw_path_item.items():
            if not isinstance(raw_method, str) or not isinstance(raw_operation, dict):
                continue
            method = raw_method.lower()
            parameters = raw_operation.get("parameters")
            if not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if not isinstance(parameter, dict) or parameter.get("in") != "header":
                    continue
                name = str(parameter.get("name", "")).lower()
                if (
                    name in _REQUIRED_HEADER_NAMES
                    or (name == "x-request-user-id" and raw_path != "/plans")
                    or (
                        name == "idempotency-key"
                        and (method, raw_path) in _REQUIRED_IDEMPOTENCY_OPERATIONS
                    )
                ):
                    parameter["required"] = True


def _add_documented_responses(schema: dict[str, object]) -> None:
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return
    for (method, path), documented_responses in _documented_api_responses().items():
        raw_path_item = paths.get(path)
        if not isinstance(raw_path_item, dict):
            continue
        raw_operation = raw_path_item.get(method)
        if not isinstance(raw_operation, dict):
            continue
        raw_responses = raw_operation.get("responses")
        if not isinstance(raw_responses, dict):
            continue
        for status_code, description in documented_responses.items():
            raw_responses.setdefault(status_code, {"description": description})


def _add_documented_response_body_schemas(schema: dict[str, object]) -> None:
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return
    for (
        method,
        path,
        status_code,
    ), body_schema in _documented_response_body_schemas().items():
        raw_path_item = paths.get(path)
        if not isinstance(raw_path_item, dict):
            continue
        raw_operation = raw_path_item.get(method)
        if not isinstance(raw_operation, dict):
            continue
        raw_responses = raw_operation.get("responses")
        if not isinstance(raw_responses, dict):
            continue
        raw_response = raw_responses.get(status_code)
        if not isinstance(raw_response, dict):
            continue
        raw_content = raw_response.setdefault("content", {})
        if not isinstance(raw_content, dict):
            continue
        raw_json_content = raw_content.setdefault("application/json", {})
        if isinstance(raw_json_content, dict):
            raw_json_content.setdefault("schema", body_schema)


def _add_documented_request_body_requirements(schema: dict[str, object]) -> None:
    paths = schema.get("paths")
    components = schema.get("components")
    if not isinstance(paths, dict) or not isinstance(components, dict):
        return
    raw_schemas = components.get("schemas")
    if not isinstance(raw_schemas, dict):
        return
    schemas = cast(dict[str, dict[str, object]], raw_schemas)
    for (method, path), required_fields in (
        _documented_request_body_required_fields().items()
    ):
        raw_path_item = paths.get(path)
        if not isinstance(raw_path_item, dict):
            continue
        raw_operation = raw_path_item.get(method)
        if not isinstance(raw_operation, dict):
            continue
        raw_schema = (
            raw_operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        if not isinstance(raw_schema, dict):
            continue
        target_schema = _resolve_openapi_schema(raw_schema, schemas)
        if target_schema is None:
            continue
        raw_required = target_schema.setdefault("required", [])
        if not isinstance(raw_required, list):
            continue
        for field_name in required_fields:
            if field_name not in raw_required:
                raw_required.append(field_name)


def _resolve_openapi_schema(
    schema: dict[str, object],
    schemas: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    raw_ref = schema.get("$ref")
    if isinstance(raw_ref, str):
        ref_name = raw_ref.rsplit("/", maxsplit=1)[-1]
        return schemas.get(ref_name)
    return schema


@lru_cache(maxsize=1)
def _documented_api_responses() -> dict[tuple[str, str], dict[str, str]]:
    documented: dict[tuple[str, str], dict[str, str]] = {}
    for method_path, raw_detail in _documented_api_details().items():
        raw_responses = raw_detail.get("responses")
        if not isinstance(raw_responses, list):
            continue
        responses: dict[str, str] = {}
        for raw_response in raw_responses:
            if not isinstance(raw_response, dict):
                continue
            raw_status = raw_response.get("status")
            if not isinstance(raw_status, int):
                continue
            raw_description = raw_response.get("description")
            description = (
                raw_description
                if isinstance(raw_description, str) and raw_description
                else "Documented response"
            )
            responses[str(raw_status)] = description
        documented[method_path] = responses
    return documented


@lru_cache(maxsize=1)
def _documented_request_body_required_fields() -> dict[
    tuple[str, str],
    tuple[str, ...],
]:
    documented: dict[tuple[str, str], tuple[str, ...]] = {}
    for method_path, raw_detail in _documented_api_details().items():
        raw_request = raw_detail.get("request")
        if not isinstance(raw_request, dict):
            continue
        raw_body_fields = raw_request.get("bodyFields")
        if not isinstance(raw_body_fields, list):
            continue
        required_fields = []
        for raw_field in raw_body_fields:
            if not isinstance(raw_field, dict) or raw_field.get("required") is not True:
                continue
            raw_name = raw_field.get("name")
            if isinstance(raw_name, str) and "." not in raw_name:
                required_fields.append(raw_name)
        if required_fields:
            documented[method_path] = tuple(required_fields)
    return documented


@lru_cache(maxsize=1)
def _documented_response_body_schemas() -> dict[
    tuple[str, str, str],
    dict[str, object],
]:
    documented: dict[tuple[str, str, str], dict[str, object]] = {}
    for (method, path), raw_detail in _documented_api_details().items():
        raw_responses = raw_detail.get("responses")
        if not isinstance(raw_responses, list):
            continue
        for raw_response in raw_responses:
            if not isinstance(raw_response, dict):
                continue
            raw_status = raw_response.get("status")
            raw_body_example = raw_response.get("bodyExample")
            if not isinstance(raw_status, int) or not isinstance(
                raw_body_example,
                dict,
            ):
                continue
            body_example = cast(dict[str, JsonValue], raw_body_example)
            documented[(method, path, str(raw_status))] = _schema_from_example(
                body_example,
            )
    return documented


def _schema_from_example(value: JsonValue) -> dict[str, object]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, list):
        item_schemas = [_schema_from_example(item) for item in value]
        items_schema = item_schemas[0] if item_schemas else {}
        return {"type": "array", "items": items_schema}
    properties = {
        name: _schema_from_example(child_value)
        for name, child_value in value.items()
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": True,
    }


@lru_cache(maxsize=1)
def _documented_api_details() -> dict[tuple[str, str], dict[str, object]]:
    try:
        raw_data = json.loads(_DOCUMENTATION_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw_data, dict):
        return {}
    raw_apis = raw_data.get("apis")
    raw_details = raw_data.get("apiDetails")
    if not isinstance(raw_apis, list) or not isinstance(raw_details, dict):
        return {}

    documented: dict[tuple[str, str], dict[str, object]] = {}
    for raw_api in raw_apis:
        if not isinstance(raw_api, dict):
            continue
        raw_api_id = raw_api.get("id")
        raw_method = raw_api.get("method")
        raw_path = raw_api.get("path")
        if not (
            isinstance(raw_api_id, str)
            and isinstance(raw_method, str)
            and isinstance(raw_path, str)
        ):
            continue
        raw_detail = raw_details.get(raw_api_id)
        if not isinstance(raw_detail, dict):
            continue
        documented[(raw_method.lower(), raw_path)] = cast(dict[str, object], raw_detail)
    return documented
