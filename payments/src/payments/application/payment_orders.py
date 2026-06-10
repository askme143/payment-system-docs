from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.ports import Clock, PaymentRepository
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.payment import Payment

IDEMPOTENCY_SCOPE = "payments-orders"


@dataclass(frozen=True, slots=True)
class PaymentOrderItem:
    sku_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class PaymentOrderResult:
    checkout_id: str
    payment_id: str
    order_id: str
    amount: int
    status: str


@dataclass(frozen=True, slots=True)
class PaymentDetail:
    id: str
    order_id: str
    amount: int
    status: str
    checkout_id: str | None
    approved_at: datetime | None
    receipt_url: str | None


async def create_payment_order(
    requester: RequestContext,
    items: list[PaymentOrderItem],
    success_url: str,
    fail_url: str,
    payment_repository: PaymentRepository,
    clock: Clock,
    idempotency_key: str | None = None,
    checkout_id: str | None = None,
) -> PaymentOrderResult:
    """일반결제 주문과 결제 시도를 생성합니다.

    Args:
        requester: 내부 백엔드가 인증해 전달한 요청 컨텍스트입니다.
        items: 구매하려는 one-time SKU와 수량 목록입니다.
        success_url: 결제 인증 성공 후 돌아갈 URL입니다.
        fail_url: 결제 인증 실패 후 돌아갈 URL입니다.
        payment_repository: 체크아웃, 결제, 멱등성 키 저장소입니다.
        clock: 생성 시각과 만료 시각을 결정하는 시간 포트입니다.
        idempotency_key: 같은 주문 생성 요청 중복 실행을 막는 선택 키입니다.
        checkout_id: 실패 또는 취소 후 재시도할 기존 체크아웃 ID입니다.

    Returns:
        결제창 호출에 필요한 주문 생성 결과입니다.

    Raises:
        AuthorizationError: 요청 사용자 ID가 없는 경우 발생합니다.
        ResourceNotFoundError: 재시도 체크아웃이 요청자 소유가 아닌 경우 발생합니다.
        InvalidStateTransitionError: 체크아웃 상태가 재시도 불가면 발생합니다.
        IdempotencyConflictError: 같은 멱등성 키가 다른 payload에 쓰인 경우입니다.
    """
    user_id = _require_user_id(requester)
    _validate_items(items)
    payload = {
        "items": [{"skuId": item.sku_id, "quantity": item.quantity} for item in items],
        "successUrl": success_url,
        "failUrl": fail_url,
        "checkoutId": checkout_id,
    }
    request_hash = _hash_payload(payload)
    key_hash = _hash_text(idempotency_key) if idempotency_key else None

    if key_hash:
        existing = await payment_repository.find_idempotency_key(
            IDEMPOTENCY_SCOPE, key_hash
        )
        if existing and existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was used with another payload"
            )
        if existing and existing.response_body:
            return PaymentOrderResult(
                checkout_id=existing.response_body["checkoutId"],
                payment_id=existing.response_body["paymentId"],
                order_id=existing.response_body["orderId"],
                amount=existing.response_body["amount"],
                status=existing.response_body["status"],
            )

    now = clock.utc_now()
    checkout = await _resolve_checkout(
        user_id=user_id,
        checkout_id=checkout_id,
        items=items,
        payment_repository=payment_repository,
        now=now,
    )
    amount = _calculate_temporary_amount(items)
    payment = Payment(
        id=Payment.generate_id(),
        order_id=f"order_{checkout.id}_{int(now.timestamp())}",
        amount=amount,
        status="ready",
        created_at=now,
        checkout_id=checkout.id,
        payment_customer_id=checkout.payment_customer_id,
        expires_at=now + timedelta(minutes=30),
    )
    checkout.last_payment_id = payment.id

    result = PaymentOrderResult(
        checkout_id=checkout.id,
        payment_id=payment.id,
        order_id=payment.order_id,
        amount=payment.amount,
        status=payment.status,
    )

    await payment_repository.save_checkout(checkout)
    await payment_repository.save_payment(payment)

    if key_hash:
        await payment_repository.save_idempotency_key(
            IdempotencyKey(
                id=IdempotencyKey.generate_id(),
                scope=IDEMPOTENCY_SCOPE,
                key_hash=key_hash,
                request_hash=request_hash,
                status="succeeded",
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=24),
                resource_type="payment",
                resource_id=payment.id,
                response_status=200,
                response_body=_result_to_response_body(result),
            )
        )

    return result


async def get_payment_detail(
    requester: RequestContext,
    payment_id: str,
    payment_repository: PaymentRepository,
) -> PaymentDetail:
    """요청 회원이 소유한 결제 상세를 조회합니다.

    Args:
        requester: 내부 백엔드가 인증해 전달한 요청 컨텍스트입니다.
        payment_id: 조회할 결제 ID입니다.
        payment_repository: 결제와 체크아웃 소유권을 함께 검증하는 저장소입니다.

    Returns:
        결제 상세 정보입니다.

    Raises:
        AuthorizationError: 요청 사용자 ID가 없는 경우 발생합니다.
        ResourceNotFoundError: 결제가 없거나 요청자 소유가 아닌 경우 발생합니다.
    """
    user_id = _require_user_id(requester)
    payment = await payment_repository.get_payment_for_user(payment_id, user_id)
    if payment is None:
        raise ResourceNotFoundError("payment not found")
    return PaymentDetail(
        id=payment.id,
        order_id=payment.order_id,
        amount=payment.amount,
        status=payment.status,
        checkout_id=payment.checkout_id,
        approved_at=payment.approved_at,
        receipt_url=payment.receipt_url,
    )


async def _resolve_checkout(
    user_id: str,
    checkout_id: str | None,
    items: list[PaymentOrderItem],
    payment_repository: PaymentRepository,
    now: datetime,
) -> Checkout:
    if checkout_id:
        checkout = await payment_repository.get_checkout_for_user(checkout_id, user_id)
        if checkout is None:
            raise ResourceNotFoundError("checkout not found")
        if checkout.status not in {"ready", "failed"}:
            raise InvalidStateTransitionError("checkout cannot be retried")
        return checkout

    return Checkout(
        id=Checkout.generate_id(),
        user_id=user_id,
        payment_customer_id=f"pcus_for_{user_id}",
        items=[{"skuId": item.sku_id, "quantity": item.quantity} for item in items],
        status="ready",
        created_at=now,
    )


def _require_user_id(requester: RequestContext) -> str:
    if not requester.user_id:
        raise AuthorizationError("request user id is required")
    return requester.user_id


def _validate_items(items: list[PaymentOrderItem]) -> None:
    if not items:
        raise InvalidStateTransitionError("items are required")
    if any(item.quantity < 1 for item in items):
        raise InvalidStateTransitionError("item quantity must be positive")


def _calculate_temporary_amount(items: list[PaymentOrderItem]) -> int:
    return sum(item.quantity for item in items) * 1000


def _hash_payload(payload: dict) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result_to_response_body(result: PaymentOrderResult) -> dict:
    return {
        "checkoutId": result.checkout_id,
        "paymentId": result.payment_id,
        "orderId": result.order_id,
        "amount": result.amount,
        "status": result.status,
    }
