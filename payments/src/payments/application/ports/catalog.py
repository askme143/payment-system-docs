from __future__ import annotations

from typing import Protocol

from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class CatalogRepository(Protocol):
    async def list_active_subscription_catalog(
        self,
    ) -> list[tuple[Product, SubscriptionPlan]]:
        raise NotImplementedError

    async def get_active_subscription_plan(
        self,
        plan_id: str,
    ) -> tuple[Product, SubscriptionPlan] | None:
        raise NotImplementedError

    async def list_user_active_product_subscriptions(
        self,
        user_id: str,
    ) -> list[Subscription]:
        raise NotImplementedError
