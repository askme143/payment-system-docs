from __future__ import annotations

from datetime import datetime
from typing import Protocol

from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan


class AdminCatalogRepository(Protocol):
    async def get_product(self, product_id: str) -> Product | None:
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
