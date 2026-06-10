from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from payments.application.ports.checkouts import CheckoutRepository
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.one_time_skus import OneTimeSkuRepository
from payments.application.ports.payment_attempts import PaymentAttemptRepository


class OneTimePaymentUnitOfWork(Protocol):
    idempotency_keys: IdempotencyKeyRepository
    checkouts: CheckoutRepository
    payments: PaymentAttemptRepository
    one_time_skus: OneTimeSkuRepository

    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class OneTimePaymentUnitOfWorkFactory(Protocol):
    def __call__(self) -> OneTimePaymentUnitOfWork:
        raise NotImplementedError
