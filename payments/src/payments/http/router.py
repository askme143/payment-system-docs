from __future__ import annotations

from fastapi import APIRouter

from payments.http.dependencies import HttpDependencies
from payments.http.routes.admin_auth import (
    create_router as create_admin_auth_router,
)
from payments.http.routes.admin_catalog import (
    create_router as create_admin_catalog_router,
)
from payments.http.routes.admin_console import (
    create_router as create_admin_console_router,
)
from payments.http.routes.admin_operations import (
    create_router as create_admin_operations_router,
)
from payments.http.routes.admin_operator_audits import (
    create_router as create_admin_operator_audits_router,
)
from payments.http.routes.admin_scheduler_runs import (
    create_router as create_admin_scheduler_runs_router,
)
from payments.http.routes.billing_auth import (
    create_router as create_billing_auth_router,
)
from payments.http.routes.billing_methods import (
    create_router as create_billing_methods_router,
)
from payments.http.routes.catalog import create_router as create_catalog_router
from payments.http.routes.internal import create_router as create_internal_router
from payments.http.routes.invoices import create_router as create_invoices_router
from payments.http.routes.payments import create_router as create_payments_router
from payments.http.routes.subscriptions import (
    create_router as create_subscriptions_router,
)
from payments.http.routes.webhooks import create_router as create_webhooks_router


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter()
    router.include_router(create_admin_auth_router(dependencies))
    router.include_router(create_admin_catalog_router(dependencies))
    router.include_router(create_admin_console_router(dependencies))
    router.include_router(create_admin_operator_audits_router(dependencies))
    router.include_router(create_admin_operations_router(dependencies))
    router.include_router(create_admin_scheduler_runs_router(dependencies))
    router.include_router(create_billing_auth_router(dependencies))
    router.include_router(create_billing_methods_router(dependencies))
    router.include_router(create_catalog_router(dependencies))
    router.include_router(create_internal_router(dependencies))
    router.include_router(create_invoices_router(dependencies))
    router.include_router(create_payments_router(dependencies))
    router.include_router(create_subscriptions_router(dependencies))
    router.include_router(create_webhooks_router(dependencies))
    return router
