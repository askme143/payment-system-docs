from __future__ import annotations

from typing import Protocol

from payments.domain.entities.payment_cancel_request import PaymentCancelRequest


class PaymentCancelRequestRepository(Protocol):
    async def find_payment_cancel_request(
        self,
        payment_id: str,
        idempotency_key_hash: str,
    ) -> PaymentCancelRequest | None:
        raise NotImplementedError

    async def save_payment_cancel_request(
        self,
        payment_cancel_request: PaymentCancelRequest,
    ) -> None:
        raise NotImplementedError
