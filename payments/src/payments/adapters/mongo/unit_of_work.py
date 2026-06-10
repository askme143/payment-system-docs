from __future__ import annotations

from types import TracebackType

from motor.motor_asyncio import (
    AsyncIOMotorClientSession,
    AsyncIOMotorDatabase,
)

from payments.adapters.mongo.checkouts import MongoCheckoutRepository
from payments.adapters.mongo.idempotency import MongoIdempotencyKeyRepository
from payments.adapters.mongo.one_time_skus import MongoOneTimeSkuRepository
from payments.adapters.mongo.payment_attempts import MongoPaymentAttemptRepository
from payments.application.ports.checkouts import CheckoutRepository
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.one_time_skus import OneTimeSkuRepository
from payments.application.ports.payment_attempts import PaymentAttemptRepository
from payments.application.ports.unit_of_work import (
    OneTimePaymentUnitOfWork,
    OneTimePaymentUnitOfWorkFactory,
)


class MongoOneTimePaymentUnitOfWork(OneTimePaymentUnitOfWork):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._session: AsyncIOMotorClientSession | None = None
        self.idempotency_keys: IdempotencyKeyRepository
        self.checkouts: CheckoutRepository
        self.payments: PaymentAttemptRepository
        self.one_time_skus: OneTimeSkuRepository

    async def __aenter__(self) -> MongoOneTimePaymentUnitOfWork:
        self._session = await self._database.client.start_session()
        self._session.start_transaction()
        self.idempotency_keys = MongoIdempotencyKeyRepository(
            self._database.idempotency_keys,
            session=self._session,
        )
        self.checkouts = MongoCheckoutRepository(
            self._database.checkouts,
            session=self._session,
        )
        self.payments = MongoPaymentAttemptRepository(
            self._database.checkouts,
            self._database.payments,
            session=self._session,
        )
        self.one_time_skus = MongoOneTimeSkuRepository(
            self._database.products,
            self._database.one_time_skus,
            session=self._session,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is None:
            await self._session.commit_transaction()
        else:
            await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


class MongoOneTimePaymentUnitOfWorkFactory(OneTimePaymentUnitOfWorkFactory):
    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database

    def __call__(self) -> MongoOneTimePaymentUnitOfWork:
        return MongoOneTimePaymentUnitOfWork(self._database)
