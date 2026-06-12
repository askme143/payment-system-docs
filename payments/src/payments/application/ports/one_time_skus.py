from __future__ import annotations

from typing import Protocol

from payments.domain.entities.one_time_sku import OneTimeSku


class OneTimeSkuRepository(Protocol):
    async def get_active_one_time_sku(self, sku_id: str) -> OneTimeSku | None:
        raise NotImplementedError

    async def reserve_one_time_sku_stock(
        self,
        sku: OneTimeSku,
        quantity: int,
    ) -> bool:
        raise NotImplementedError

    async def release_reserved_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        raise NotImplementedError

    async def capture_reserved_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        raise NotImplementedError

    async def restore_sold_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        raise NotImplementedError
