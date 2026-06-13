from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan


@dataclass(frozen=True, slots=True)
class AdminProductQuery:
    product_type: str | None = None
    status: tuple[str, ...] | None = None
    keyword: str | None = None
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class AdminProductListRecord:
    product: Product
    subscription_plan_count: int
    active_subscription_plan_count: int
    one_time_sku_count: int
    active_one_time_sku_count: int


class AdminCatalogRepository(Protocol):
    async def list_products(
        self,
        query: AdminProductQuery,
    ) -> list[AdminProductListRecord]:
        raise NotImplementedError

    async def get_product(self, product_id: str) -> Product | None:
        raise NotImplementedError

    async def list_subscription_plans(
        self,
        product_id: str,
    ) -> list[SubscriptionPlan]:
        raise NotImplementedError

    async def list_one_time_skus(
        self,
        product_id: str,
    ) -> list[OneTimeSku]:
        raise NotImplementedError

    async def list_product_audit_records(
        self,
        product_id: str,
        child_ids: tuple[str, ...],
        limit: int,
    ) -> list[OperatorAudit]:
        raise NotImplementedError

    async def get_product_by_code(
        self,
        product_code: str,
        product_type: str,
    ) -> Product | None:
        raise NotImplementedError

    async def save_product(self, product: Product) -> None:
        raise NotImplementedError

    async def count_active_subscription_plans(self, product_id: str) -> int:
        raise NotImplementedError

    async def count_active_one_time_skus(self, product_id: str) -> int:
        raise NotImplementedError

    async def get_subscription_plan(
        self,
        product_id: str,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        raise NotImplementedError

    async def get_subscription_plan_by_code(
        self,
        product_id: str,
        plan_code: str,
    ) -> SubscriptionPlan | None:
        raise NotImplementedError

    async def save_subscription_plan(self, plan: SubscriptionPlan) -> None:
        raise NotImplementedError

    async def get_one_time_sku(
        self,
        product_id: str,
        sku_id: str,
    ) -> OneTimeSku | None:
        raise NotImplementedError

    async def get_one_time_sku_by_code(
        self,
        product_id: str,
        sku_code: str,
    ) -> OneTimeSku | None:
        raise NotImplementedError

    async def save_one_time_sku(self, sku: OneTimeSku) -> None:
        raise NotImplementedError

    async def save_product_audit_record(
        self,
        *,
        product_id: str,
        admin_id: str,
        request_id: str,
        action: str,
        previous: dict[str, object] | None,
        next_value: dict[str, object],
        request_ip: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        raise NotImplementedError
