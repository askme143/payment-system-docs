from __future__ import annotations

from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.errors import DuplicateKeyError

from payments.adapters.mongo.documents import from_document, to_document
from payments.application.cursors import decode_cursor
from payments.application.errors import InvalidStateTransitionError
from payments.application.ports.admin_catalog import (
    AdminProductListRecord,
    AdminProductQuery,
)
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.product import Product
from payments.domain.entities.subscription_plan import SubscriptionPlan


class MongoAdminCatalogRepository:
    def __init__(
        self,
        products: AsyncIOMotorCollection,
        operator_audits: AsyncIOMotorCollection,
        subscription_plans: AsyncIOMotorCollection,
        one_time_skus: AsyncIOMotorCollection,
    ) -> None:
        self._products = products
        self._operator_audits = operator_audits
        self._subscription_plans = subscription_plans
        self._one_time_skus = one_time_skus

    async def list_products(
        self,
        query: AdminProductQuery,
    ) -> list[AdminProductListRecord]:
        filters = _product_query_filter(query)
        cursor = self._products.find(filters).sort(
            [("product_code", 1), ("_id", 1)]
        ).limit(query.limit)
        records: list[AdminProductListRecord] = []
        async for document in cursor:
            product = from_document(Product, document)
            if product is None:
                continue
            records.append(
                AdminProductListRecord(
                    product=product,
                    subscription_plan_count=(
                        await self._subscription_plans.count_documents(
                            {"product_id": product.id}
                        )
                    ),
                    active_subscription_plan_count=(
                        await self._subscription_plans.count_documents(
                            {"product_id": product.id, "status": "active"}
                        )
                    ),
                    one_time_sku_count=(
                        await self._one_time_skus.count_documents(
                            {"product_id": product.id}
                        )
                    ),
                    active_one_time_sku_count=(
                        await self._one_time_skus.count_documents(
                            {"product_id": product.id, "status": "active"}
                        )
                    ),
                )
            )
        return records

    async def get_product(self, product_id: str) -> Product | None:
        document = await self._products.find_one({"_id": product_id})
        return from_document(Product, document)

    async def list_subscription_plans(
        self,
        product_id: str,
    ) -> list[SubscriptionPlan]:
        cursor = self._subscription_plans.find({"product_id": product_id}).sort(
            [("plan_code", 1), ("_id", 1)]
        )
        return [
            plan
            for document in [document async for document in cursor]
            if (plan := from_document(SubscriptionPlan, document)) is not None
        ]

    async def list_one_time_skus(
        self,
        product_id: str,
    ) -> list[OneTimeSku]:
        cursor = self._one_time_skus.find({"product_id": product_id}).sort(
            [("sku_code", 1), ("_id", 1)]
        )
        return [
            sku
            for document in [document async for document in cursor]
            if (sku := from_document(OneTimeSku, document)) is not None
        ]

    async def list_product_audit_records(
        self,
        product_id: str,
        child_ids: tuple[str, ...],
        limit: int,
    ) -> list[OperatorAudit]:
        target_ids = [product_id, *child_ids]
        cursor = (
            self._operator_audits.find(
                {
                    "target_type": {
                        "$in": ["product", "subscription_plan", "one_time_sku"]
                    },
                    "target_id": {"$in": target_ids},
                }
            )
            .sort([("created_at", -1), ("_id", -1)])
            .limit(limit)
        )
        return [
            audit
            for document in [document async for document in cursor]
            if (audit := from_document(OperatorAudit, document)) is not None
        ]

    async def get_product_by_code(
        self,
        product_code: str,
        product_type: str,
    ) -> Product | None:
        document = await self._products.find_one(
            {"product_code": product_code, "product_type": product_type}
        )
        return from_document(Product, document)

    async def save_product(self, product: Product) -> None:
        try:
            await self._products.replace_one(
                {"_id": product.id},
                to_document(product),
                upsert=True,
            )
        except DuplicateKeyError as exc:
            raise InvalidStateTransitionError("product code already exists") from exc

    async def count_active_subscription_plans(self, product_id: str) -> int:
        return await self._subscription_plans.count_documents(
            {"product_id": product_id, "status": "active"}
        )

    async def count_active_one_time_skus(self, product_id: str) -> int:
        return await self._one_time_skus.count_documents(
            {"product_id": product_id, "status": "active"}
        )

    async def get_subscription_plan(
        self,
        product_id: str,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        document = await self._subscription_plans.find_one(
            {"_id": plan_id, "product_id": product_id}
        )
        return from_document(SubscriptionPlan, document)

    async def get_subscription_plan_by_code(
        self,
        product_id: str,
        plan_code: str,
    ) -> SubscriptionPlan | None:
        document = await self._subscription_plans.find_one(
            {"product_id": product_id, "plan_code": plan_code}
        )
        return from_document(SubscriptionPlan, document)

    async def save_subscription_plan(self, plan: SubscriptionPlan) -> None:
        document = to_document(plan)
        document.pop("currency", None)
        try:
            await self._subscription_plans.replace_one(
                {"_id": plan.id},
                document,
                upsert=True,
            )
        except DuplicateKeyError as exc:
            raise InvalidStateTransitionError(
                "subscription plan code already exists"
            ) from exc

    async def get_one_time_sku(
        self,
        product_id: str,
        sku_id: str,
    ) -> OneTimeSku | None:
        document = await self._one_time_skus.find_one(
            {"_id": sku_id, "product_id": product_id}
        )
        return from_document(OneTimeSku, document)

    async def get_one_time_sku_by_code(
        self,
        product_id: str,
        sku_code: str,
    ) -> OneTimeSku | None:
        document = await self._one_time_skus.find_one(
            {"product_id": product_id, "sku_code": sku_code}
        )
        return from_document(OneTimeSku, document)

    async def save_one_time_sku(self, sku: OneTimeSku) -> None:
        document = to_document(sku, omit_none=True)
        document.pop("currency", None)
        try:
            await self._one_time_skus.replace_one(
                {"_id": sku.id},
                document,
                upsert=True,
            )
        except DuplicateKeyError as exc:
            raise InvalidStateTransitionError(
                "one-time sku code already exists"
            ) from exc

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
        target_type, target_id = _audit_target(action, product_id, next_value)
        reason_message = _audit_reason_message(next_value)
        audit_created_at = created_at or datetime.now(UTC)
        await self._operator_audits.replace_one(
            {"_id": f"{request_id}:{action}:{product_id}"},
            {
                "_id": f"{request_id}:{action}:{product_id}",
                "operator_id": admin_id,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "previous_state": previous or {},
                "next_state": next_value,
                "reason_code": action,
                **(
                    {"reason_message": reason_message}
                    if reason_message is not None
                    else {}
                ),
                **({"request_ip": request_ip} if request_ip is not None else {}),
                "result": "succeeded",
                "created_at": audit_created_at,
            },
            upsert=True,
        )


def _audit_target(
    action: str,
    product_id: str,
    next_value: dict[str, object],
) -> tuple[str, str]:
    if action.startswith("subscription_plan."):
        return "subscription_plan", str(next_value.get("plan_id", product_id))
    if action.startswith("one_time_sku."):
        return "one_time_sku", str(next_value.get("sku_id", product_id))
    return "product", product_id


def _audit_reason_message(next_value: dict[str, object]) -> str | None:
    reason = next_value.get("change_reason") or next_value.get("reason")
    if reason is None:
        return None
    return str(reason)


def _product_query_filter(query: AdminProductQuery) -> dict[str, object]:
    filters: dict[str, object] = {}
    clauses: list[dict[str, object]] = []
    if query.product_type is not None:
        filters["product_type"] = query.product_type
    if query.status is not None:
        filters["status"] = {"$in": list(query.status)}
    if query.keyword is not None:
        clauses.append(
            {
                "$or": [
                    {"product_code": {"$regex": query.keyword, "$options": "i"}},
                    {"name": {"$regex": query.keyword, "$options": "i"}},
                ]
            }
        )
    if query.cursor is not None:
        payload = decode_cursor(query.cursor)
        product_code = str(payload["productCode"])
        product_id = str(payload["productId"])
        clauses.append(
            {
                "$or": [
                    {"product_code": {"$gt": product_code}},
                    {"product_code": product_code, "_id": {"$gt": product_id}},
                ]
            }
        )
    if clauses:
        filters["$and"] = clauses
    return filters
