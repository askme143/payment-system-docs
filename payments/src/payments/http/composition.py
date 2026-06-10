from __future__ import annotations

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from payments.adapters.mongo.catalog import MongoCatalogRepository
from payments.adapters.mongo.payment_attempts import MongoPaymentAttemptRepository
from payments.adapters.mongo.unit_of_work import MongoOneTimePaymentUnitOfWorkFactory
from payments.adapters.time import SystemClock
from payments.http.config import PaymentHttpConfig
from payments.http.dependencies import HttpDependencies
from payments.http.errors import register_error_handlers
from payments.http.router import create_router


def build_http_dependencies(
    database: AsyncIOMotorDatabase,
    config: PaymentHttpConfig,
) -> HttpDependencies:
    return HttpDependencies(
        catalog_repository=MongoCatalogRepository(
            database.products,
            database.subscription_plans,
        ),
        one_time_payment_uow_factory=MongoOneTimePaymentUnitOfWorkFactory(database),
        payment_attempts=MongoPaymentAttemptRepository(
            checkouts=database.checkouts,
            payments=database.payments,
        ),
        clock=SystemClock(),
        internal_service_token=config.internal_service_token,
    )


def create_app(dependencies: HttpDependencies) -> FastAPI:
    app = FastAPI(title="Payment System API")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    register_error_handlers(app)
    app.include_router(create_router(dependencies))
    return app


def create_mongo_database(config: PaymentHttpConfig) -> AsyncIOMotorDatabase:
    from datetime import UTC

    client = AsyncIOMotorClient(config.database_url, tz_aware=True, tzinfo=UTC)
    return client[config.database_name]
