from __future__ import annotations

from dataclasses import Field, fields, is_dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol, cast

MongoDocument = dict[str, object]


class DataclassInstance(Protocol):
    __dataclass_fields__: ClassVar[dict[str, Field[object]]]


def to_document(entity: object) -> MongoDocument:
    if not _is_dataclass_instance(entity):
        raise TypeError("entity must be a dataclass instance")
    dataclass_entity = cast(DataclassInstance, entity)
    data = {
        field.name: _to_mongo_value(getattr(dataclass_entity, field.name))
        for field in fields(dataclass_entity)
    }
    data["_id"] = data.pop("id")
    return {key: _to_mongo_value(value) for key, value in data.items()}


def from_document[T](entity_type: type[T], document: MongoDocument | None) -> T | None:
    if document is None:
        return None
    data = dict(document)
    data["id"] = data.pop("_id")
    dataclass_type = cast(type[DataclassInstance], entity_type)
    field_names = {field.name for field in fields(dataclass_type)}
    return cast(
        T,
        entity_type(
            **{
                key: _from_mongo_value(value)
                for key, value in data.items()
                if key in field_names
            }
        ),
    )


def _is_dataclass_instance(value: object) -> bool:
    return is_dataclass(value) and not isinstance(value, type)


def _to_mongo_value(value: object) -> object:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    if isinstance(value, list):
        return [_to_mongo_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_mongo_value(item) for key, item in value.items()}
    return value


def _from_mongo_value(value: object) -> object:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    if isinstance(value, list):
        return [_from_mongo_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _from_mongo_value(item) for key, item in value.items()}
    return value
