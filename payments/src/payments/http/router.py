from __future__ import annotations

from fastapi import APIRouter

from payments.http.dependencies import HttpDependencies
from payments.http.routes.catalog import create_router as create_catalog_router
from payments.http.routes.payments import create_router as create_payments_router


def create_router(dependencies: HttpDependencies) -> APIRouter:
    router = APIRouter()
    router.include_router(create_catalog_router(dependencies))
    router.include_router(create_payments_router(dependencies))
    return router
