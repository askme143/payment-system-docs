from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from payments.application.errors import (
    BadRequestError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.ports.admin_catalog import AdminCatalogRepository
from payments.application.ports.clock import Clock
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan

ProductType = Literal["subscription", "one_time"]
ProductStatus = Literal["draft", "active", "paused", "archived"]
BillingPeriod = Literal["monthly", "yearly"]
StockPolicy = Literal["unlimited", "limited"]
_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AdminRequestContext:
    request_id: str
    admin_id: str
    request_ip: str | None = None


@dataclass(frozen=True, slots=True)
class AdminProductCreateCommand:
    product_code: str
    product_type: ProductType
    name: str
    status: ProductStatus = "draft"


@dataclass(frozen=True, slots=True)
class AdminProductStatusChangeCommand:
    status: object | None
    reason: object | None
    effective_at: object | None = None


@dataclass(frozen=True, slots=True)
class AdminProductStatusChangeResult:
    product: Product
    previous_status: ProductStatus
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class AdminSubscriptionPlanCreateCommand:
    plan_code: str
    billing_period: str
    amount: object
    currency: str = "KRW"
    status: str = "draft"
    entitlements: object | None = None


@dataclass(frozen=True, slots=True)
class AdminSubscriptionPlanUpdateCommand:
    change_reason: str
    amount: object | None = None
    currency: str | None = None
    status: str | None = None
    entitlements: object | None = None


@dataclass(frozen=True, slots=True)
class AdminSubscriptionPlanUpdateResult:
    plan: SubscriptionPlan
    product_type: ProductType
    effective_for: str


@dataclass(frozen=True, slots=True)
class AdminOneTimeSkuCreateCommand:
    sku_code: str
    amount: object
    currency: str = "KRW"
    status: str = "draft"
    stock_policy: object | None = None
    total_stock: object | None = None
    purchase_limit: object | None = None


@dataclass(frozen=True, slots=True)
class AdminOneTimeSkuUpdateCommand:
    change_reason: str
    amount: object | None = None
    currency: str | None = None
    status: str | None = None
    stock_policy: object | None = None
    total_stock: object | None = None
    purchase_limit: object | None = None


@dataclass(frozen=True, slots=True)
class AdminOneTimeSkuUpdateResult:
    sku: OneTimeSku
    product_type: ProductType
    effective_for: str


async def create_admin_product(
    command: AdminProductCreateCommand,
    context: AdminRequestContext,
    repository: AdminCatalogRepository,
) -> Product:
    """관리자가 공통 Product를 생성합니다.

    Args:
        command: 생성할 Product 공통 속성입니다.
        context: 인증된 관리자 요청 컨텍스트입니다.
        repository: Product와 운영 감사 로그를 저장하는 저장소입니다.

    Returns:
        생성된 Product 엔티티입니다.

    Raises:
        InvalidStateTransitionError: 같은 productCode가 이미 존재할 때 발생합니다.
    """
    _validate_product_create(command)
    existing = await repository.get_product_by_code(
        command.product_code,
        command.product_type,
    )
    if existing is not None:
        raise InvalidStateTransitionError("product code already exists")

    product = Product(
        id=Product.generate_product_id(),
        product_code=command.product_code,
        product_type=command.product_type,
        name=command.name,
        status=command.status,
    )
    await repository.save_product(product)
    await repository.save_product_audit_record(
        product_id=product.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        action="product.create",
        previous=None,
        next_value=_product_audit_value(product),
        request_ip=context.request_ip,
    )
    return product


async def change_admin_product_status(
    product_id: str,
    command: AdminProductStatusChangeCommand,
    context: AdminRequestContext,
    repository: AdminCatalogRepository,
    clock: Clock,
) -> AdminProductStatusChangeResult:
    """관리자가 공통 Product 판매 상태를 전환합니다.

    Args:
        product_id: 상태를 전환할 Product ID입니다.
        command: 목표 상태와 운영 사유입니다.
        context: 인증된 관리자 요청 컨텍스트입니다.
        repository: Product와 운영 감사 로그를 저장하는 저장소입니다.
        clock: 현재 시각을 제공하는 포트입니다.

    Returns:
        상태 전환 결과입니다.

    Raises:
        ResourceNotFoundError: Product가 없을 때 발생합니다.
        InvalidStateTransitionError: 허용되지 않는 전환이거나 active 전환에
            필요한 판매 단위가 없을 때 발생합니다.
    """
    status, reason, effective_at = _validate_product_status_change(command, clock)
    product = await repository.get_product(product_id)
    if product is None:
        raise ResourceNotFoundError("product not found")
    _validate_product_status_transition(product.status, status)
    if product.status == "archived" and status != "archived":
        raise InvalidStateTransitionError("archived product cannot be reactivated")
    if status == "active":
        await _require_active_selling_unit(product, repository)

    previous_status = product.status
    product.status = status
    await repository.save_product(product)
    await repository.save_product_audit_record(
        product_id=product.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        action="product.status_change",
        previous={"status": previous_status},
        next_value={
            "status": product.status,
            "reason": reason,
            "effective_at": effective_at,
        },
        request_ip=context.request_ip,
        created_at=clock.utc_now(),
    )
    return AdminProductStatusChangeResult(
        product=product,
        previous_status=previous_status,
        effective_at=effective_at,
    )


async def create_admin_subscription_plan(
    product_id: str,
    command: AdminSubscriptionPlanCreateCommand,
    context: AdminRequestContext,
    repository: AdminCatalogRepository,
) -> SubscriptionPlan:
    """관리자가 구독상품 Product에 구독 플랜을 추가합니다.

    Args:
        product_id: 플랜을 추가할 Product ID입니다.
        command: 생성할 구독 플랜 정책입니다.
        context: 인증된 관리자 요청 컨텍스트입니다.
        repository: Product, 플랜, 운영 감사 로그 저장소입니다.

    Returns:
        생성된 구독 플랜입니다.

    Raises:
        ResourceNotFoundError: Product가 없을 때 발생합니다.
        InvalidStateTransitionError: Product 타입이 맞지 않거나 같은 planCode가
            이미 존재할 때 발생합니다.
    """
    billing_period, amount, status, entitlements = _validate_subscription_plan_create(
        command
    )
    product = await _get_subscription_product(product_id, repository)
    existing = await repository.get_subscription_plan_by_code(
        product.id,
        command.plan_code,
    )
    if existing is not None:
        raise InvalidStateTransitionError("subscription plan code already exists")

    plan = SubscriptionPlan(
        id=SubscriptionPlan.generate_plan_id(),
        product_id=product.id,
        plan_code=command.plan_code,
        billing_period=billing_period,
        amount=amount,
        entitlements=entitlements,
        status=status,
        currency=command.currency,
    )
    await repository.save_subscription_plan(plan)
    await repository.save_product_audit_record(
        product_id=product.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        action="subscription_plan.create",
        previous=None,
        next_value=_subscription_plan_audit_value(plan),
        request_ip=context.request_ip,
    )
    return plan


async def update_admin_subscription_plan(
    product_id: str,
    plan_id: str,
    command: AdminSubscriptionPlanUpdateCommand,
    context: AdminRequestContext,
    repository: AdminCatalogRepository,
) -> AdminSubscriptionPlanUpdateResult:
    """관리자가 구독 플랜의 신규 가입 및 다음 주기 정책을 수정합니다.

    Args:
        product_id: 플랜이 속한 Product ID입니다.
        plan_id: 수정할 구독 플랜 ID입니다.
        command: 변경할 정책과 변경 사유입니다.
        context: 인증된 관리자 요청 컨텍스트입니다.
        repository: Product, 플랜, 운영 감사 로그 저장소입니다.

    Returns:
        수정된 플랜과 적용 범위입니다.

    Raises:
        ResourceNotFoundError: Product나 플랜이 없을 때 발생합니다.
        InvalidStateTransitionError: Product 타입이 맞지 않을 때 발생합니다.
    """
    amount, status, entitlements = _validate_subscription_plan_update(command)
    product = await _get_subscription_product(product_id, repository)
    plan = await repository.get_subscription_plan(product.id, plan_id)
    if plan is None:
        raise ResourceNotFoundError("subscription plan not found")

    previous = _subscription_plan_audit_value(plan)
    if amount is not None:
        plan.amount = amount
    if command.currency is not None:
        plan.currency = command.currency
    if status is not None:
        plan.status = status
    if entitlements is not None:
        plan.entitlements = entitlements
    plan.version += 1
    await repository.save_subscription_plan(plan)
    await repository.save_product_audit_record(
        product_id=product.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        action="subscription_plan.update",
        previous=previous,
        next_value={
            **_subscription_plan_audit_value(plan),
            "change_reason": command.change_reason,
        },
        request_ip=context.request_ip,
    )
    return AdminSubscriptionPlanUpdateResult(
        plan=plan,
        product_type=product.product_type,
        effective_for="new_subscriptions_and_next_cycles",
    )


async def create_admin_one_time_sku(
    product_id: str,
    command: AdminOneTimeSkuCreateCommand,
    context: AdminRequestContext,
    repository: AdminCatalogRepository,
) -> OneTimeSku:
    """관리자가 일반상품 Product에 단건 구매 SKU를 추가합니다.

    Args:
        product_id: SKU를 추가할 Product ID입니다.
        command: 생성할 SKU 판매 정책입니다.
        context: 인증된 관리자 요청 컨텍스트입니다.
        repository: Product, SKU, 운영 감사 로그 저장소입니다.

    Returns:
        생성된 SKU입니다.

    Raises:
        ResourceNotFoundError: Product가 없을 때 발생합니다.
        InvalidStateTransitionError: Product 타입이 맞지 않거나 같은 skuCode가
            이미 존재할 때 발생합니다.
    """
    amount, status, stock_policy, total_stock, purchase_limit = (
        _validate_one_time_sku_create(command)
    )
    product = await _get_one_time_product(product_id, repository)
    existing = await repository.get_one_time_sku_by_code(product.id, command.sku_code)
    if existing is not None:
        raise InvalidStateTransitionError("one-time sku code already exists")

    sku = OneTimeSku(
        id=OneTimeSku.generate_id(),
        product_id=product.id,
        sku_code=command.sku_code,
        amount=amount,
        stock_policy=stock_policy,
        status=status,
        currency=command.currency,
        purchase_limit=purchase_limit,
        total_stock=total_stock if stock_policy == "limited" else None,
        reserved_stock=0 if stock_policy == "limited" else None,
        sold_stock=0 if stock_policy == "limited" else None,
    )
    _validate_stock_policy(sku)
    await repository.save_one_time_sku(sku)
    await repository.save_product_audit_record(
        product_id=product.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        action="one_time_sku.create",
        previous=None,
        next_value=_one_time_sku_audit_value(sku),
        request_ip=context.request_ip,
    )
    return sku


async def update_admin_one_time_sku(
    product_id: str,
    sku_id: str,
    command: AdminOneTimeSkuUpdateCommand,
    context: AdminRequestContext,
    repository: AdminCatalogRepository,
) -> AdminOneTimeSkuUpdateResult:
    """관리자가 일반상품 SKU의 새 주문 정책을 수정합니다.

    Args:
        product_id: SKU가 속한 Product ID입니다.
        sku_id: 수정할 SKU ID입니다.
        command: 변경할 판매, 가격, 재고 정책과 변경 사유입니다.
        context: 인증된 관리자 요청 컨텍스트입니다.
        repository: Product, SKU, 운영 감사 로그 저장소입니다.

    Returns:
        수정된 SKU와 적용 범위입니다.

    Raises:
        ResourceNotFoundError: Product나 SKU가 없을 때 발생합니다.
        InvalidStateTransitionError: Product 타입 또는 재고 정책이 충돌할 때
            발생합니다.
    """
    amount, status, stock_policy, total_stock, purchase_limit = (
        _validate_one_time_sku_update(command)
    )
    product = await _get_one_time_product(product_id, repository)
    sku = await repository.get_one_time_sku(product.id, sku_id)
    if sku is None:
        raise ResourceNotFoundError("one-time sku not found")

    previous = _one_time_sku_audit_value(sku)
    updated_sku = replace(sku)
    if amount is not None:
        updated_sku.amount = amount
    if command.currency is not None:
        updated_sku.currency = command.currency
    if status is not None:
        updated_sku.status = status
    if stock_policy is not None:
        updated_sku.stock_policy = stock_policy
    if purchase_limit is not None:
        updated_sku.purchase_limit = purchase_limit
    _apply_stock_update(updated_sku, total_stock)
    _validate_stock_policy_transition(sku, updated_sku, stock_policy, total_stock)
    await repository.save_one_time_sku(updated_sku)
    await repository.save_product_audit_record(
        product_id=product.id,
        admin_id=context.admin_id,
        request_id=context.request_id,
        action="one_time_sku.update",
        previous=previous,
        next_value={
            **_one_time_sku_audit_value(updated_sku),
            "change_reason": command.change_reason,
        },
        request_ip=context.request_ip,
    )
    return AdminOneTimeSkuUpdateResult(
        sku=updated_sku,
        product_type=product.product_type,
        effective_for="new_orders",
    )


async def _require_active_selling_unit(
    product: Product,
    repository: AdminCatalogRepository,
) -> None:
    if product.product_type == "subscription":
        active_count = await repository.count_active_subscription_plans(product.id)
    else:
        active_count = await repository.count_active_one_time_skus(product.id)
    if active_count < 1:
        raise InvalidStateTransitionError("active selling unit is required")


def _validate_product_create(command: AdminProductCreateCommand) -> None:
    if not command.product_code.strip():
        raise BadRequestError("productCode is required")
    if not _CODE_PATTERN.fullmatch(command.product_code):
        raise BadRequestError("productCode format is invalid")
    if not command.name.strip():
        raise BadRequestError("name is required")
    if command.status != "draft":
        raise BadRequestError("product status must start as draft")


def _validate_product_status_change(
    command: AdminProductStatusChangeCommand,
    clock: Clock,
) -> tuple[ProductStatus, str, datetime]:
    if not isinstance(command.status, str):
        raise BadRequestError("status is required")
    status = _validate_product_status(command.status)
    if not isinstance(command.reason, str) or not command.reason.strip():
        raise BadRequestError("reason is required")
    effective_at = _validate_effective_at(command.effective_at, clock)
    return status, command.reason.strip(), effective_at


def _validate_product_status_transition(
    current_status: ProductStatus,
    target_status: ProductStatus,
) -> None:
    if current_status != "draft" and target_status == "draft":
        raise InvalidStateTransitionError("product cannot return to draft")


def _validate_effective_at(value: object | None, clock: Clock) -> datetime:
    if value is None:
        return clock.utc_now()
    if isinstance(value, datetime):
        effective_at = value
    elif isinstance(value, str):
        try:
            effective_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BadRequestError("effectiveAt is invalid") from exc
    else:
        raise BadRequestError("effectiveAt is invalid")
    if effective_at.tzinfo is None or effective_at.utcoffset() is None:
        raise BadRequestError("effectiveAt is invalid")
    return effective_at


def _validate_subscription_plan_create(
    command: AdminSubscriptionPlanCreateCommand,
) -> tuple[BillingPeriod, int, ProductStatus, dict[str, object]]:
    if not command.plan_code.strip():
        raise BadRequestError("planCode is required")
    if not _CODE_PATTERN.fullmatch(command.plan_code):
        raise BadRequestError("planCode format is invalid")
    billing_period = _validate_billing_period(command.billing_period)
    status = _validate_product_status(command.status)
    amount = _validate_positive_amount(command.amount)
    _validate_currency(command.currency)
    entitlements = _validate_entitlements(command.entitlements)
    return billing_period, amount, status, entitlements


def _validate_subscription_plan_update(
    command: AdminSubscriptionPlanUpdateCommand,
) -> tuple[int | None, ProductStatus | None, dict[str, object] | None]:
    if not command.change_reason.strip():
        raise BadRequestError("changeReason is required")
    if (
        command.amount is None
        and command.currency is None
        and command.status is None
        and command.entitlements is None
    ):
        raise BadRequestError("no subscription plan fields to update")
    amount = (
        _validate_positive_amount(command.amount)
        if command.amount is not None
        else None
    )
    if command.currency is not None:
        _validate_currency(command.currency)
    status = (
        _validate_product_status(command.status)
        if command.status is not None
        else None
    )
    entitlements = (
        _validate_entitlements(command.entitlements)
        if command.entitlements is not None
        else None
    )
    return amount, status, entitlements


def _validate_one_time_sku_create(
    command: AdminOneTimeSkuCreateCommand,
) -> tuple[int, ProductStatus, StockPolicy, int | None, dict[str, object] | None]:
    if not command.sku_code.strip():
        raise BadRequestError("skuCode is required")
    if not _CODE_PATTERN.fullmatch(command.sku_code):
        raise BadRequestError("skuCode format is invalid")
    amount = _validate_positive_amount(command.amount)
    _validate_currency(command.currency)
    status = _validate_product_status(command.status)
    stock_policy = _validate_stock_policy_request(command.stock_policy)
    total_stock = _validate_optional_positive_int(command.total_stock, "totalStock")
    if stock_policy == "limited" and total_stock is None:
        raise BadRequestError("totalStock must be positive for limited SKU")
    if stock_policy == "unlimited" and total_stock is not None:
        raise BadRequestError("totalStock is only valid for limited SKU")
    purchase_limit = _validate_purchase_limit(command.purchase_limit)
    return amount, status, stock_policy, total_stock, purchase_limit


def _validate_one_time_sku_update(
    command: AdminOneTimeSkuUpdateCommand,
) -> tuple[
    int | None,
    ProductStatus | None,
    StockPolicy | None,
    int | None,
    dict[str, object] | None,
]:
    if not command.change_reason.strip():
        raise BadRequestError("changeReason is required")
    if (
        command.amount is None
        and command.currency is None
        and command.status is None
        and command.stock_policy is None
        and command.total_stock is None
        and command.purchase_limit is None
    ):
        raise BadRequestError("no SKU fields to update")
    amount = (
        _validate_positive_amount(command.amount)
        if command.amount is not None
        else None
    )
    if command.currency is not None:
        _validate_currency(command.currency)
    status = (
        _validate_product_status(command.status)
        if command.status is not None
        else None
    )
    stock_policy = (
        _validate_stock_policy_request(command.stock_policy)
        if command.stock_policy is not None
        else None
    )
    total_stock = _validate_optional_positive_int(command.total_stock, "totalStock")
    purchase_limit = _validate_purchase_limit(command.purchase_limit)
    return amount, status, stock_policy, total_stock, purchase_limit


def _validate_positive_amount(amount: object) -> int:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        raise BadRequestError("amount must be positive")
    return amount


def _validate_currency(currency: str) -> None:
    if currency != "KRW":
        raise BadRequestError("currency is invalid")


def _validate_billing_period(value: str) -> BillingPeriod:
    if value not in ("monthly", "yearly"):
        raise BadRequestError("billingPeriod is invalid")
    return value


def _validate_product_status(value: str) -> ProductStatus:
    if value not in ("draft", "active", "paused", "archived"):
        raise BadRequestError("status is invalid")
    return value


def _validate_stock_policy_request(value: object | None) -> StockPolicy:
    if value is None:
        return "unlimited"
    if not isinstance(value, dict):
        raise BadRequestError("stockPolicy is invalid")
    stock_policy_type = value.get("type")
    if stock_policy_type not in ("unlimited", "limited"):
        raise BadRequestError("stockPolicy is invalid")
    return stock_policy_type


def _validate_optional_positive_int(
    value: object | None,
    field_name: str,
) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BadRequestError(f"{field_name} must be positive")
    return value


def _validate_entitlements(entitlements: object | None) -> dict[str, object]:
    if entitlements is None:
        return {}
    if not isinstance(entitlements, dict):
        raise BadRequestError("entitlements is invalid")
    validated: dict[str, object] = {}
    for key, value in entitlements.items():
        if not isinstance(key, str):
            raise BadRequestError("entitlements is invalid")
        validated[key] = value
    return validated


def _validate_purchase_limit(
    purchase_limit: object | None,
) -> dict[str, object] | None:
    if purchase_limit is None:
        return None
    if not isinstance(purchase_limit, dict):
        raise BadRequestError("purchaseLimit is invalid")
    if not purchase_limit:
        raise BadRequestError("purchaseLimit is invalid")
    allowed_keys = {"perUser", "perOrder"}
    if any(key not in allowed_keys for key in purchase_limit):
        raise BadRequestError("purchaseLimit is invalid")
    for value in purchase_limit.values():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise BadRequestError("purchaseLimit is invalid")
    return purchase_limit


async def _get_subscription_product(
    product_id: str,
    repository: AdminCatalogRepository,
) -> Product:
    product = await repository.get_product(product_id)
    if product is None:
        raise ResourceNotFoundError("product not found")
    if product.product_type != "subscription":
        raise InvalidStateTransitionError("product type must be subscription")
    return product


async def _get_one_time_product(
    product_id: str,
    repository: AdminCatalogRepository,
) -> Product:
    product = await repository.get_product(product_id)
    if product is None:
        raise ResourceNotFoundError("product not found")
    if product.product_type != "one_time":
        raise InvalidStateTransitionError("product type must be one_time")
    return product


def _apply_stock_update(
    sku: OneTimeSku,
    total_stock: int | None,
) -> None:
    if sku.stock_policy == "unlimited":
        sku.total_stock = None
        sku.reserved_stock = None
        sku.sold_stock = None
        return
    sku.reserved_stock = sku.reserved_stock or 0
    sku.sold_stock = sku.sold_stock or 0
    if total_stock is not None:
        sku.total_stock = total_stock


def _validate_stock_policy(sku: OneTimeSku) -> None:
    if sku.stock_policy == "unlimited":
        return
    if sku.total_stock is None:
        raise InvalidStateTransitionError("total stock is required")
    reserved_stock = sku.reserved_stock or 0
    sold_stock = sku.sold_stock or 0
    if sku.total_stock < reserved_stock + sold_stock:
        raise InvalidStateTransitionError("total stock is below committed stock")


def _validate_stock_policy_transition(
    previous: OneTimeSku,
    updated: OneTimeSku,
    requested_stock_policy: StockPolicy | None,
    requested_total_stock: int | None,
) -> None:
    committed_stock = (previous.reserved_stock or 0) + (previous.sold_stock or 0)
    target_stock_policy = requested_stock_policy or previous.stock_policy
    if requested_total_stock is not None and target_stock_policy == "unlimited":
        raise BadRequestError("totalStock is only valid for limited SKU")
    if (
        previous.stock_policy == "limited"
        and requested_stock_policy == "unlimited"
        and committed_stock > 0
    ):
        raise InvalidStateTransitionError("stock policy conflicts with committed stock")
    if (
        previous.stock_policy == "unlimited"
        and requested_stock_policy == "limited"
        and requested_total_stock is None
    ):
        raise BadRequestError("totalStock must be positive for limited SKU")
    _validate_stock_policy(updated)


def _product_audit_value(product: Product) -> dict[str, object]:
    return {
        "product_id": product.id,
        "product_code": product.product_code,
        "product_type": product.product_type,
        "name": product.name,
        "status": product.status,
    }


def _subscription_plan_audit_value(plan: SubscriptionPlan) -> dict[str, object]:
    return {
        "plan_id": plan.id,
        "product_id": plan.product_id,
        "plan_code": plan.plan_code,
        "billing_period": plan.billing_period,
        "amount": plan.amount,
        "currency": plan.currency,
        "status": plan.status,
        "entitlements": plan.entitlements,
        "version": plan.version,
    }


def _one_time_sku_audit_value(sku: OneTimeSku) -> dict[str, object]:
    return {
        "sku_id": sku.id,
        "product_id": sku.product_id,
        "sku_code": sku.sku_code,
        "amount": sku.amount,
        "currency": sku.currency,
        "stock_policy": sku.stock_policy,
        "status": sku.status,
        "purchase_limit": sku.purchase_limit,
        "total_stock": sku.total_stock,
        "reserved_stock": sku.reserved_stock,
        "sold_stock": sku.sold_stock,
    }
