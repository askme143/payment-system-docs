from __future__ import annotations

import inspect

from payments import application
from payments.application.errors import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentApplicationError,
    ResourceNotFoundError,
)


class TestApplicationContract:
    def test_application_errors_share_base_type(self) -> None:
        for error_type in (
            AuthenticationError,
            AuthorizationError,
            IdempotencyConflictError,
            InvalidStateTransitionError,
            ResourceNotFoundError,
        ):
            assert issubclass(error_type, PaymentApplicationError)

    def test_public_use_cases_are_coroutines_with_contract_docstrings(self) -> None:
        for function_name in (
            "create_payment_order",
            "get_payment_detail",
            "get_subscription_plan",
            "list_subscription_plans",
        ):
            function = getattr(application, function_name)
            docstring = function.__doc__ or ""

            assert inspect.iscoroutinefunction(function)
            assert "Args:" in docstring
            assert "Returns:" in docstring
            assert "Raises:" in docstring

    def test_payment_order_signature_names_dependencies_explicitly(self) -> None:
        signature = inspect.signature(application.create_payment_order)

        assert "one_time_payment_uow_factory" in signature.parameters
        assert "clock" in signature.parameters
        assert "idempotency_key" in signature.parameters

    def test_catalog_signature_names_repository_explicitly(self) -> None:
        signature = inspect.signature(application.list_subscription_plans)

        assert list(signature.parameters) == ["catalog_repository"]
