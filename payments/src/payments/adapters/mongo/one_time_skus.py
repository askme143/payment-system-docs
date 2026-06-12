from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection

from payments.adapters.mongo.documents import from_document
from payments.application.ports.one_time_skus import OneTimeSkuRepository
from payments.domain.entities.one_time_sku import OneTimeSku


class MongoOneTimeSkuRepository(OneTimeSkuRepository):
    def __init__(
        self,
        products: AsyncIOMotorCollection,
        one_time_skus: AsyncIOMotorCollection,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._products = products
        self._one_time_skus = one_time_skus
        self._session = session

    async def get_active_one_time_sku(self, sku_id: str) -> OneTimeSku | None:
        sku_document = await self._one_time_skus.find_one(
            {"_id": sku_id, "status": "active"},
            session=self._session,
        )
        sku = from_document(OneTimeSku, sku_document)
        if sku is None:
            return None
        if not await self._has_active_one_time_product(sku.product_id):
            return None
        return sku

    async def reserve_one_time_sku_stock(
        self,
        sku: OneTimeSku,
        quantity: int,
    ) -> bool:
        if not await self._has_active_one_time_product(sku.product_id):
            return False
        if sku.stock_policy == "unlimited":
            sku_document = await self._one_time_skus.find_one(
                {
                    "_id": sku.id,
                    "status": "active",
                    "stock_policy": "unlimited",
                },
                session=self._session,
            )
            return sku_document is not None
        result = await self._one_time_skus.update_one(
            {
                "_id": sku.id,
                "status": "active",
                "stock_policy": "limited",
                "$expr": {
                    "$gte": [
                        {
                            "$subtract": [
                                "$total_stock",
                                {"$add": ["$reserved_stock", "$sold_stock"]},
                            ]
                        },
                        quantity,
                    ]
                },
            },
            {"$inc": {"reserved_stock": quantity}},
            session=self._session,
        )
        if result.modified_count != 1:
            return False
        if await self._has_active_one_time_product(sku.product_id):
            return True
        await self.release_reserved_one_time_sku_stock(sku.id, quantity)
        return False

    async def release_reserved_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        if quantity < 1:
            return
        await self._one_time_skus.update_one(
            {
                "_id": sku_id,
                "stock_policy": "limited",
                "reserved_stock": {"$gte": quantity},
            },
            {"$inc": {"reserved_stock": -quantity}},
            session=self._session,
        )

    async def capture_reserved_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        if quantity < 1:
            return
        await self._one_time_skus.update_one(
            {
                "_id": sku_id,
                "stock_policy": "limited",
                "reserved_stock": {"$gte": quantity},
            },
            {
                "$inc": {
                    "reserved_stock": -quantity,
                    "sold_stock": quantity,
                }
            },
            session=self._session,
        )

    async def restore_sold_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        if quantity < 1:
            return
        await self._one_time_skus.update_one(
            {
                "_id": sku_id,
                "stock_policy": "limited",
                "sold_stock": {"$gte": quantity},
            },
            {"$inc": {"sold_stock": -quantity}},
            session=self._session,
        )

    async def _has_active_one_time_product(self, product_id: str) -> bool:
        product = await self._products.find_one(
            {
                "_id": product_id,
                "product_type": "one_time",
                "status": "active",
            },
            session=self._session,
        )
        return product is not None
