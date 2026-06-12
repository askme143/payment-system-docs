from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document
from payments.domain.entities.product import Product
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan


class MongoCatalogRepository:
    def __init__(
        self,
        products: AsyncIOMotorCollection,
        subscription_plans: AsyncIOMotorCollection,
        subscriptions: AsyncIOMotorCollection,
    ) -> None:
        self._products = products
        self._subscription_plans = subscription_plans
        self._subscriptions = subscriptions

    async def list_active_subscription_catalog(
        self,
    ) -> list[tuple[Product, SubscriptionPlan]]:
        rows: list[tuple[Product, SubscriptionPlan]] = []
        cursor = self._subscription_plans.find({"status": "active"})
        async for plan_document in cursor:
            plan = from_document(SubscriptionPlan, plan_document)
            if plan is None:
                continue
            product_document = await self._products.find_one(
                {
                    "_id": plan.product_id,
                    "product_type": "subscription",
                    "status": "active",
                }
            )
            product = from_document(Product, product_document)
            if product is not None:
                rows.append((product, plan))
        return rows

    async def get_active_subscription_plan(
        self,
        plan_id: str,
    ) -> tuple[Product, SubscriptionPlan] | None:
        plan_document = await self._subscription_plans.find_one(
            {"_id": plan_id, "status": "active"}
        )
        plan = from_document(SubscriptionPlan, plan_document)
        if plan is None:
            return None

        product_document = await self._products.find_one(
            {
                "_id": plan.product_id,
                "product_type": "subscription",
                "status": "active",
            }
        )
        product = from_document(Product, product_document)
        if product is None:
            return None
        return product, plan

    async def list_user_active_product_subscriptions(
        self,
        user_id: str,
    ) -> list[Subscription]:
        rows: list[Subscription] = []
        cursor = self._subscriptions.find(
            {
                "user_id": user_id,
                "status": {
                    "$in": ["pending", "active", "past_due", "cancel_scheduled"]
                },
            }
        )
        async for document in cursor:
            subscription = from_document(Subscription, document)
            if subscription is not None:
                rows.append(subscription)
        return rows
