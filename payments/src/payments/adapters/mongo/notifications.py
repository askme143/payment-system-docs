from __future__ import annotations

from datetime import datetime
from typing import cast

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorCollection
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from payments.adapters.mongo.documents import MongoDocument, from_document, to_document
from payments.application.errors import IdempotencyConflictError
from payments.domain.entities.notification import (
    NotificationLastError,
    NotificationOutboxItem,
    NotificationTemplate,
)

ASCENDING = 1
DESCENDING = -1


class MongoNotificationOutboxRepository:
    def __init__(
        self,
        collection: AsyncIOMotorCollection,
        *,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._collection = collection
        self._session = session

    async def enqueue_idempotently(
        self,
        item: NotificationOutboxItem,
    ) -> NotificationOutboxItem:
        existing = await self._collection.find_one(
            {"idempotency_key": item.idempotency_key},
            session=self._session,
        )
        if existing is not None:
            existing_item = _outbox_from_document(existing)
            if existing_item.idempotency_payload_hash != item.idempotency_payload_hash:
                raise IdempotencyConflictError(
                    "notification idempotency key was used with another payload"
                )
            return existing_item
        try:
            await self._collection.replace_one(
                {"_id": item.id},
                _outbox_to_document(item),
                upsert=True,
                session=self._session,
            )
        except DuplicateKeyError as exc:
            existing_after_duplicate = await self._collection.find_one(
                {"idempotency_key": item.idempotency_key},
                session=self._session,
            )
            if existing_after_duplicate is None:
                raise
            existing_item = _outbox_from_document(existing_after_duplicate)
            if existing_item.idempotency_payload_hash != item.idempotency_payload_hash:
                raise IdempotencyConflictError(
                    "notification idempotency key was used with another payload"
                ) from exc
            return existing_item
        return item

    async def claim_due_notifications(
        self,
        *,
        now: datetime,
        lock_until: datetime,
        worker_id: str,
        limit: int,
    ) -> list[NotificationOutboxItem]:
        cursor = (
            self._collection.find(
                _claim_filter(now),
                session=self._session,
            )
            .sort([("created_at", ASCENDING), ("_id", ASCENDING)])
            .limit(limit)
        )
        claimed: list[NotificationOutboxItem] = []
        async for candidate in cursor:
            claimed_document = await self._collection.find_one_and_update(
                {
                    "_id": candidate["_id"],
                    **_claim_filter(now),
                },
                {
                    "$set": {
                        "status": "processing",
                        "worker_id": worker_id,
                        "locked_until_at": lock_until,
                        "updated_at": now,
                    },
                    "$inc": {"attempt_count": 1},
                },
                return_document=ReturnDocument.AFTER,
                session=self._session,
            )
            if claimed_document is not None:
                claimed.append(_outbox_from_document(claimed_document))
        return claimed

    async def mark_sent(
        self,
        item_id: str,
        *,
        provider_message_id: str,
        sent_at: datetime,
        purge_after_at: datetime,
    ) -> None:
        await self._collection.update_one(
            {"_id": item_id},
            {
                "$set": {
                    "status": "sent",
                    "provider_message_id": provider_message_id,
                    "sent_at": sent_at,
                    "updated_at": sent_at,
                    "purge_after_at": purge_after_at,
                },
                "$unset": {"locked_until_at": "", "worker_id": ""},
            },
            session=self._session,
        )

    async def schedule_retry(
        self,
        item_id: str,
        *,
        available_at: datetime,
        last_error: NotificationLastError,
    ) -> None:
        await self._collection.update_one(
            {"_id": item_id},
            {
                "$set": {
                    "status": "retry_scheduled",
                    "available_at": available_at,
                    "updated_at": last_error.occurred_at,
                    "last_error": _last_error_to_document(last_error),
                },
                "$unset": {"locked_until_at": ""},
            },
            session=self._session,
        )

    async def mark_dead_letter(
        self,
        item_id: str,
        *,
        last_error: NotificationLastError,
        purge_after_at: datetime,
    ) -> None:
        await self._collection.update_one(
            {"_id": item_id},
            {
                "$set": {
                    "status": "dead_letter",
                    "updated_at": last_error.occurred_at,
                    "last_error": _last_error_to_document(last_error),
                    "purge_after_at": purge_after_at,
                },
                "$unset": {"locked_until_at": ""},
            },
            session=self._session,
        )


class MongoNotificationTemplateRepository:
    def __init__(
        self,
        collection: AsyncIOMotorCollection,
        *,
        session: AsyncIOMotorClientSession | None = None,
    ) -> None:
        self._collection = collection
        self._session = session

    async def resolve_active_template(
        self,
        *,
        event_type: str,
        product_code: str | None,
        product_type: str | None,
    ) -> NotificationTemplate | None:
        candidate_keys = []
        if product_code is not None:
            candidate_keys.append(f"{product_code}.{event_type}")
        if product_type is not None:
            candidate_keys.append(f"{product_type}.{event_type}")
        candidate_keys.append(f"default.{event_type}")

        for template_key in candidate_keys:
            cursor = self._collection.find(
                {
                    "event_type": event_type,
                    "template_key": template_key,
                    "status": "active",
                },
                session=self._session,
            ).sort("version", DESCENDING)
            candidates = [
                _template_from_document(document)
                async for document in cursor
            ]
            if candidates:
                return max(candidates, key=lambda template: template.version)
        return None

    async def get_template(
        self,
        *,
        template_key: str,
        version: int,
    ) -> NotificationTemplate | None:
        return _template_from_document_or_none(
            await self._collection.find_one(
                {"template_key": template_key, "version": version},
                session=self._session,
            )
        )

    async def count_templates(self) -> int:
        return await self._collection.count_documents({}, session=self._session)

    async def save_template(self, template: NotificationTemplate) -> None:
        await self._collection.replace_one(
            {"_id": template.id},
            to_document(template, omit_none=True),
            upsert=True,
            session=self._session,
        )


def _claim_filter(now: datetime) -> MongoDocument:
    return {
        "status": {"$in": ["pending", "retry_scheduled"]},
        "available_at": {"$lte": now},
        "$or": [
            {"locked_until_at": None},
            {"locked_until_at": {"$lt": now}},
        ],
    }


def _outbox_to_document(item: NotificationOutboxItem) -> MongoDocument:
    document = to_document(item, omit_none=True)
    if item.last_error is not None:
        document["last_error"] = _last_error_to_document(item.last_error)
    return document


def _outbox_from_document(document: MongoDocument) -> NotificationOutboxItem:
    copied = dict(document)
    for optional_field in (
        "recipient_user_id",
        "recipient_admin_id",
        "product_code",
        "product_type",
    ):
        copied.setdefault(optional_field, None)
    last_error = copied.get("last_error")
    if isinstance(last_error, dict):
        copied["last_error"] = _last_error_from_document(last_error)
    item = from_document(NotificationOutboxItem, copied)
    if item is None:
        raise ValueError("notification outbox document is required")
    return item


def _template_from_document(document: MongoDocument) -> NotificationTemplate:
    template = _template_from_document_or_none(document)
    if template is None:
        raise ValueError("notification template document is required")
    return template


def _template_from_document_or_none(
    document: MongoDocument | None,
) -> NotificationTemplate | None:
    if document is not None:
        copied = dict(document)
        copied.setdefault("product_code", None)
        copied.setdefault("product_type", None)
        return from_document(NotificationTemplate, copied)
    return from_document(NotificationTemplate, document)


def _last_error_to_document(last_error: NotificationLastError) -> MongoDocument:
    return {
        "code": last_error.code,
        "message": last_error.message,
        "retryable": last_error.retryable,
        "occurred_at": last_error.occurred_at,
    }


def _last_error_from_document(document: dict[str, object]) -> NotificationLastError:
    return NotificationLastError(
        code=str(document["code"]),
        message=str(document["message"]),
        retryable=bool(document["retryable"]),
        occurred_at=cast(datetime, document["occurred_at"]),
    )
