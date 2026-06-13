from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIRoute

from conftest import (
    FakeAdminAuthEmailSender,
    FakeAdminAuthRateLimiter,
    FakeAdminAuthRepository,
    FakeAdminAuthUnitOfWorkFactory,
    FakeAdminCatalogRepository,
    FakeAdminOperationsRepository,
    FakeAdminSubscriptionAdjustUnitOfWorkFactory,
    FakeBillingAuthIssueUnitOfWorkFactory,
    FakeBillingAuthRepository,
    FakeBillingMethodDefaultUnitOfWorkFactory,
    FakeBillingMethodDeleteUnitOfWorkFactory,
    FakeBillingMethodRepository,
    FakeBillingRetryRepository,
    FakeCatalogRepository,
    FakeCheckoutRepository,
    FakeIdempotencyKeyRepository,
    FakeInvoiceRepository,
    FakeOneTimePaymentUnitOfWorkFactory,
    FakeOneTimeSkuRepository,
    FakeOperationLockRepository,
    FakeOperatorAuditRepository,
    FakePaymentAttemptRepository,
    FakePaymentCancelRequestRepository,
    FakePaymentCustomerRepository,
    FakePaymentProvider,
    FakePaymentStores,
    FakeSchedulerRunRepository,
    FakeSubscriptionAccountRepository,
    FakeSubscriptionBillingUnitOfWorkFactory,
    FakeSubscriptionCancelUnitOfWorkFactory,
    FakeSubscriptionChangeTokenCodec,
    FakeSubscriptionChangeUnitOfWorkFactory,
    FakeSubscriptionCheckoutRepository,
    FakeSubscriptionConfirmUnitOfWorkFactory,
    FakeSubscriptionExpirationRepository,
    FakeSubscriptionExpirationUnitOfWorkFactory,
    FakeSubscriptionResumeUnitOfWorkFactory,
    FakeWebhookRepository,
    FakeWebhookUnitOfWorkFactory,
    FixedClock,
    fake_notification_enqueue_dependencies,
)
from payments.adapters.crypto import FernetBillingKeyCipher
from payments.http.composition import create_app
from payments.http.dependencies import HttpDependencies

IMPLEMENTED_API_IDS = {
    "admin-auth-login",
    "admin-auth-login-confirm",
    "admin-auth-logout",
    "admin-auth-me",
    "admin-auth-password-reset-confirm",
    "admin-auth-password-reset-request",
    "admin-auth-refresh",
    "plans-list",
    "plans-detail",
    "payments-confirm",
    "payments-orders",
    "payments-auth-result",
    "payments-cancel",
    "payments-detail",
    "internal-billing-run",
    "internal-billing-retry",
    "subscriptions-me",
    "billing-methods",
    "billing-method-default",
    "billing-method-delete",
    "invoices-list",
    "invoices-detail",
    "subscriptions-cancel",
    "subscriptions-resume",
    "admin-products-create",
    "admin-products-detail",
    "admin-products-list",
    "admin-products-status",
    "admin-scheduler-runs-create",
    "admin-scheduler-runs-detail",
    "admin-scheduler-runs-list",
    "admin-operator-audits-detail",
    "admin-operator-audits-list",
    "admin-subscription-plans-create",
    "admin-subscription-plans-update",
    "admin-one-time-skus-create",
    "admin-one-time-skus-update",
    "admin-payments",
    "admin-payment-cancel",
    "admin-subscription-adjust",
    "admin-subscriptions",
    "billing-auth",
    "billing-issue",
    "subscriptions-checkout",
    "subscriptions-confirm",
    "subscriptions-change",
    "subscriptions-change-preview",
    "webhooks-toss-payments",
}


def test_first_slice_routes_match_documentation() -> None:
    docs_path = Path(__file__).resolve().parents[2] / "docs-data" / "documentation.json"
    data = json.loads(docs_path.read_text())
    documented_routes = {
        (api["method"], api["path"])
        for api in data["apis"]
        if api["id"] in IMPLEMENTED_API_IDS
    }
    checkouts = FakeCheckoutRepository()
    invoices = FakeInvoiceRepository()
    payment_attempts = FakePaymentAttemptRepository(checkouts)
    payment_stores = FakePaymentStores(
        idempotency_keys=FakeIdempotencyKeyRepository(),
        checkouts=checkouts,
        invoices=invoices,
        payments=payment_attempts,
        one_time_skus=FakeOneTimeSkuRepository(),
        payment_customers=FakePaymentCustomerRepository(),
        payment_cancel_requests=FakePaymentCancelRequestRepository(),
        operator_audits=FakeOperatorAuditRepository(),
    )
    billing_auths = FakeBillingAuthRepository()
    subscription_checkouts = FakeSubscriptionCheckoutRepository()
    subscription_accounts = FakeSubscriptionAccountRepository()
    subscription_expirations = FakeSubscriptionExpirationRepository()
    billing_methods = FakeBillingMethodRepository()
    billing_retries = FakeBillingRetryRepository()
    admin_auth = FakeAdminAuthRepository()
    admin_operations = FakeAdminOperationsRepository()
    webhooks = FakeWebhookRepository()
    app = create_app(
        HttpDependencies(
            admin_catalog=FakeAdminCatalogRepository(),
            admin_auth=admin_auth,
            admin_auth_uow_factory=FakeAdminAuthUnitOfWorkFactory(admin_auth),
            admin_auth_email_sender=FakeAdminAuthEmailSender(),
            admin_auth_rate_limiter=FakeAdminAuthRateLimiter(),
            admin_operations=admin_operations,
            operator_audits=payment_stores.operator_audits,
            scheduler_runs=FakeSchedulerRunRepository(),
            admin_subscription_adjust_uow_factory=(
                FakeAdminSubscriptionAdjustUnitOfWorkFactory(
                    admin_operations=admin_operations,
                    idempotency_keys=payment_stores.idempotency_keys,
                )
            ),
            billing_auths=billing_auths,
            billing_auth_issue_uow_factory=FakeBillingAuthIssueUnitOfWorkFactory(
                billing_auths=billing_auths,
                idempotency_keys=payment_stores.idempotency_keys,
            ),
            catalog_repository=FakeCatalogRepository(),
            billing_methods=billing_methods,
            billing_method_default_uow_factory=(
                FakeBillingMethodDefaultUnitOfWorkFactory(billing_methods)
            ),
            billing_method_delete_uow_factory=(
                FakeBillingMethodDeleteUnitOfWorkFactory(
                    billing_methods=billing_methods,
                    idempotency_keys=payment_stores.idempotency_keys,
                    operator_audits=payment_stores.operator_audits,
                )
            ),
            billing_retries=billing_retries,
            invoices=invoices,
            idempotency_keys=payment_stores.idempotency_keys,
            operation_locks=FakeOperationLockRepository(),
            one_time_payment_uow_factory=FakeOneTimePaymentUnitOfWorkFactory(
                payment_stores
            ),
            payment_attempts=payment_attempts,
            payment_customers=payment_stores.payment_customers,
            payment_provider=FakePaymentProvider(),
            subscription_accounts=subscription_accounts,
            subscription_billing_uow_factory=(
                FakeSubscriptionBillingUnitOfWorkFactory(billing_retries)
            ),
            subscription_change_tokens=FakeSubscriptionChangeTokenCodec(),
            subscription_checkouts=subscription_checkouts,
            subscription_cancel_uow_factory=FakeSubscriptionCancelUnitOfWorkFactory(
                subscriptions=subscription_accounts,
                idempotency_keys=payment_stores.idempotency_keys,
                operator_audits=payment_stores.operator_audits,
            ),
            subscription_change_uow_factory=FakeSubscriptionChangeUnitOfWorkFactory(
                billing_repository=billing_retries,
                subscriptions=subscription_accounts,
                idempotency_keys=payment_stores.idempotency_keys,
                operator_audits=payment_stores.operator_audits,
            ),
            subscription_confirm_uow_factory=FakeSubscriptionConfirmUnitOfWorkFactory(
                billing_auths=billing_auths,
                subscriptions=subscription_checkouts,
                idempotency_keys=payment_stores.idempotency_keys,
            ),
            subscription_expirations=subscription_expirations,
            subscription_expiration_uow_factory=(
                FakeSubscriptionExpirationUnitOfWorkFactory(
                    subscriptions=subscription_expirations,
                    operator_audits=payment_stores.operator_audits,
                )
            ),
            subscription_resume_uow_factory=FakeSubscriptionResumeUnitOfWorkFactory(
                subscriptions=subscription_accounts,
                idempotency_keys=payment_stores.idempotency_keys,
                operator_audits=payment_stores.operator_audits,
            ),
            notification_enqueue=fake_notification_enqueue_dependencies(),
            webhooks=webhooks,
            webhook_uow_factory=FakeWebhookUnitOfWorkFactory(webhooks),
            billing_key_cipher=FernetBillingKeyCipher("test-billing-key-secret"),
            clock=FixedClock(),
            internal_service_token="secret",
            toss_client_key="test_ck_local",
        )
    )
    app_routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST", "PATCH", "DELETE"}
    }

    assert documented_routes <= app_routes

    documented_optional_bodies = {
        "admin-auth-logout": {"adminRefreshToken"},
        "internal-billing-run": {"jobType", "billingDate", "limit", "dryRun"},
        "internal-billing-retry": {"force", "reason", "dryRun"},
        "subscriptions-cancel": {"cancelReason", "feedback"},
        "subscriptions-resume": {"resumeReason"},
    }
    api_by_id = {api["id"]: api for api in data["apis"]}
    openapi = app.openapi()
    schemas = openapi["components"]["schemas"]
    for api_id, expected_fields in documented_optional_bodies.items():
        api = api_by_id[api_id]
        operation = openapi["paths"][api["path"]][api["method"].lower()]
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        actual_fields = _schema_properties(body, schemas)
        assert expected_fields <= actual_fields

    for api_id in IMPLEMENTED_API_IDS:
        api = api_by_id[api_id]
        detail = data["apiDetails"][api_id]
        operation = openapi["paths"][api["path"]][api["method"].lower()]
        documented_required_headers = {
            header["name"].lower()
            for header in detail.get("request", {}).get("headers", [])
            if header.get("required") is True and header["name"] != "Content-Type"
        }
        openapi_required_headers = {
            parameter["name"].lower()
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "header" and parameter.get("required") is True
        }
        assert documented_required_headers <= openapi_required_headers
        documented_query_params = _documented_query_param_names(detail)
        openapi_query_params = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "query"
        }
        assert documented_query_params <= openapi_query_params
        request_surface_names = _openapi_request_surface_names(operation, schemas)
        assert _documented_frontend_input_names(detail) <= request_surface_names
        documented_response_statuses = {
            str(response["status"])
            for response in detail.get("responses", [])
            if isinstance(response.get("status"), int)
        }
        openapi_response_statuses = set(operation.get("responses", {}))
        assert documented_response_statuses <= openapi_response_statuses

    webhook_api = api_by_id["webhooks-toss-payments"]
    webhook_operation = openapi["paths"][webhook_api["path"]][
        webhook_api["method"].lower()
    ]
    webhook_required_headers = {
        parameter["name"].lower()
        for parameter in webhook_operation.get("parameters", [])
        if parameter.get("in") == "header" and parameter.get("required") is True
    }
    assert "toss-signature" in webhook_required_headers

    resume_api = api_by_id["subscriptions-resume"]
    resume_operation = openapi["paths"][resume_api["path"]][
        resume_api["method"].lower()
    ]
    resume_response = resume_operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    resume_fields = _schema_properties(resume_response, schemas)
    assert {
        "subscriptionId",
        "status",
        "cancelAt",
        "currentPeriodEnd",
        "nextBillingDate",
        "resumeAvailable",
    } <= resume_fields
    assert "accessUntil" not in resume_fields

    adjust_api = api_by_id["admin-subscription-adjust"]
    adjust_operation = openapi["paths"][adjust_api["path"]][
        adjust_api["method"].lower()
    ]
    adjust_body = adjust_operation["requestBody"]["content"]["application/json"][
        "schema"
    ]
    postpone_by_schema = _schema_property_schema(adjust_body, schemas, "postponeBy")
    assert "days" in _schema_properties(postpone_by_schema, schemas)

    dynamic_response_prefixes = (
        "previousState.",
        "currentState.",
        "paymentFailure.",
        "failure.",
    )
    for api_id in IMPLEMENTED_API_IDS:
        api = api_by_id[api_id]
        detail = data["apiDetails"][api_id]
        operation = openapi["paths"][api["path"]][api["method"].lower()]
        body_fields = detail.get("request", {}).get("bodyFields", [])
        expected_body_paths = {field["name"] for field in body_fields}
        if expected_body_paths:
            request_body = operation["requestBody"]["content"]["application/json"][
                "schema"
            ]
            assert expected_body_paths <= _schema_paths(request_body, schemas)
            expected_required_body_fields = {
                field["name"]
                for field in body_fields
                if field.get("required") is True and "." not in field["name"]
            }
            assert expected_required_body_fields <= _schema_required_properties(
                request_body,
                schemas,
            )

        for response in detail.get("responses", []):
            body_example = response.get("bodyExample")
            if not isinstance(body_example, dict):
                continue
            response_schema = (
                operation.get("responses", {})
                .get(str(response["status"]), {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            assert isinstance(response_schema, dict)
            expected_response_paths = {
                path
                for path in _example_paths(body_example)
                if not path.startswith(dynamic_response_prefixes)
            }
            assert expected_response_paths <= _schema_paths(response_schema, schemas)


def _schema_properties(
    schema: dict[str, object],
    schemas: dict[str, dict[str, object]],
) -> set[str]:
    if "$ref" in schema:
        ref_name = str(schema["$ref"]).rsplit("/", maxsplit=1)[-1]
        return _schema_properties(schemas[ref_name], schemas)
    raw_any_of = schema.get("anyOf")
    if isinstance(raw_any_of, list):
        properties: set[str] = set()
        for candidate in raw_any_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                properties.update(_schema_properties(candidate, schemas))
        return properties
    raw_properties = schema.get("properties")
    if isinstance(raw_properties, dict):
        return set(raw_properties)
    return set()


def _schema_required_properties(
    schema: dict[str, object],
    schemas: dict[str, dict[str, object]],
) -> set[str]:
    if "$ref" in schema:
        ref_name = str(schema["$ref"]).rsplit("/", maxsplit=1)[-1]
        return _schema_required_properties(schemas[ref_name], schemas)
    raw_any_of = schema.get("anyOf")
    if isinstance(raw_any_of, list):
        required: set[str] = set()
        for candidate in raw_any_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                required.update(_schema_required_properties(candidate, schemas))
        return required
    raw_required = schema.get("required")
    if isinstance(raw_required, list):
        return {item for item in raw_required if isinstance(item, str)}
    return set()


def _documented_query_param_names(detail: dict[str, object]) -> set[str]:
    raw_inputs = detail.get("frontendInputs")
    if not isinstance(raw_inputs, list):
        return set()
    names: set[str] = set()
    for raw_input in raw_inputs:
        if not isinstance(raw_input, dict) or raw_input.get("source") != "query":
            continue
        raw_name = raw_input.get("name")
        if not isinstance(raw_name, str):
            continue
        names.update(name.strip() for name in raw_name.split(",") if name.strip())
    return names


def _documented_frontend_input_names(detail: dict[str, object]) -> set[str]:
    raw_inputs = detail.get("frontendInputs")
    if not isinstance(raw_inputs, list):
        return set()
    names: set[str] = set()
    for raw_input in raw_inputs:
        if not isinstance(raw_input, dict):
            continue
        if raw_input.get("source") == "serverResponse":
            continue
        raw_name = raw_input.get("name")
        if not isinstance(raw_name, str):
            continue
        names.update(name.strip() for name in raw_name.split(",") if name.strip())
    return names


def _openapi_request_surface_names(
    operation: dict[str, object],
    schemas: dict[str, dict[str, object]],
) -> set[str]:
    names: set[str] = set()
    raw_parameters = operation.get("parameters")
    if isinstance(raw_parameters, list):
        for parameter in raw_parameters:
            if not isinstance(parameter, dict):
                continue
            raw_name = parameter.get("name")
            if (
                parameter.get("in") in {"path", "query", "header"}
                and isinstance(raw_name, str)
            ):
                names.add(raw_name)
    raw_body_schema = _operation_request_body_schema(operation)
    if isinstance(raw_body_schema, dict):
        names.update(_schema_paths(raw_body_schema, schemas))
    return names


def _operation_request_body_schema(
    operation: dict[str, object],
) -> dict[str, object] | None:
    raw_request_body = operation.get("requestBody")
    if not isinstance(raw_request_body, dict):
        return None
    raw_content = raw_request_body.get("content")
    if not isinstance(raw_content, dict):
        return None
    raw_json_content = raw_content.get("application/json")
    if not isinstance(raw_json_content, dict):
        return None
    raw_schema = raw_json_content.get("schema")
    if isinstance(raw_schema, dict):
        return raw_schema
    return None


def _schema_paths(
    schema: dict[str, object],
    schemas: dict[str, dict[str, object]],
    prefix: str = "",
) -> set[str]:
    if "$ref" in schema:
        ref_name = str(schema["$ref"]).rsplit("/", maxsplit=1)[-1]
        return _schema_paths(schemas[ref_name], schemas, prefix)
    paths: set[str] = set()
    raw_properties = schema.get("properties")
    if isinstance(raw_properties, dict):
        for name, child_schema in raw_properties.items():
            path = f"{prefix}.{name}" if prefix else name
            paths.add(path)
            if isinstance(child_schema, dict):
                paths.update(_schema_paths(child_schema, schemas, path))
    raw_any_of = schema.get("anyOf")
    if isinstance(raw_any_of, list):
        for candidate in raw_any_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                paths.update(_schema_paths(candidate, schemas, prefix))
    if schema.get("type") == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            paths.update(_schema_paths(items, schemas, prefix))
    return paths


def _example_paths(
    value: dict[str, object],
    prefix: str = "",
) -> set[str]:
    paths: set[str] = set()
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else key
        paths.add(path)
        if isinstance(child, dict):
            paths.update(_example_paths(child, path))
    return paths


def _schema_property_schema(
    schema: dict[str, object],
    schemas: dict[str, dict[str, object]],
    property_name: str,
) -> dict[str, object]:
    if "$ref" in schema:
        ref_name = str(schema["$ref"]).rsplit("/", maxsplit=1)[-1]
        return _schema_property_schema(schemas[ref_name], schemas, property_name)
    raw_any_of = schema.get("anyOf")
    if isinstance(raw_any_of, list):
        for candidate in raw_any_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                return _schema_property_schema(candidate, schemas, property_name)
    raw_properties = schema.get("properties")
    if isinstance(raw_properties, dict):
        property_schema = raw_properties[property_name]
        if isinstance(property_schema, dict):
            return property_schema
    raise AssertionError(f"schema property not found: {property_name}")
