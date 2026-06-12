from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType

import pytest

from payments.application.billing_methods import (
    BillingMethodRecord,
    delete_billing_method,
    get_user_billing_methods,
    set_default_billing_method,
)
from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.ports.billing_methods import (
    BillingKeyStatus,
    BillingMethodStatus,
)
from payments.application.ports.unit_of_work import (
    BillingMethodDefaultUnitOfWork,
    BillingMethodDefaultUnitOfWorkFactory,
)
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.operator_audit import OperatorAudit


class FakeBillingMethodRepository:
    def __init__(self) -> None:
        self.records: dict[str, list[BillingMethodRecord]] = {}
        self.method_owners: dict[str, str] = {}
        self.active_subscription_counts: dict[str, int] = {}
        self.default_changed_at = datetime(2026, 6, 8, 10, 20, tzinfo=UTC)
        self.raise_on_set_default = False
        self.raise_on_deactivate = False

    async def list_active_billing_methods_for_user(
        self,
        user_id: str,
    ) -> list[BillingMethodRecord]:
        return [
            record
            for record in self.records.get(user_id, [])
            if record.status == "active"
        ]

    async def count_active_subscriptions_for_user(self, user_id: str) -> int:
        return self.active_subscription_counts.get(user_id, 0)

    async def get_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
    ) -> BillingMethodRecord | None:
        return next(
            (
                record
                for record in self.records.get(user_id, [])
                if record.billing_method_id == billing_method_id
            ),
            None,
        )

    async def get_any_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
    ) -> BillingMethodRecord | None:
        return next(
            (
                record
                for record in self.records.get(user_id, [])
                if record.billing_method_id == billing_method_id
            ),
            None,
        )

    async def get_billing_method_owner(self, billing_method_id: str) -> str | None:
        for user_id, records in self.records.items():
            if any(record.billing_method_id == billing_method_id for record in records):
                return user_id
        return self.method_owners.get(billing_method_id)

    async def set_default_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
        changed_at: datetime,
    ) -> str | None:
        if self.raise_on_set_default:
            raise LookupError("billing method was not defaultable")
        previous_default_id: str | None = None
        updated_records: list[BillingMethodRecord] = []
        for record in self.records.get(user_id, []):
            if record.is_default:
                previous_default_id = record.billing_method_id
            updated_records.append(
                BillingMethodRecord(
                    billing_method_id=record.billing_method_id,
                    status=record.status,
                    is_default=record.billing_method_id == billing_method_id,
                    method=record.method,
                    card_company=record.card_company,
                    masked_card_number=record.masked_card_number,
                    billing_key_status=record.billing_key_status,
                    created_at=record.created_at,
                )
            )
        self.records[user_id] = updated_records
        self.default_changed_at = changed_at
        return previous_default_id

    async def deactivate_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
        deleted_at: datetime,
    ) -> None:
        if self.raise_on_deactivate:
            raise LookupError("billing method was not deletable")
        updated_records: list[BillingMethodRecord] = []
        for record in self.records.get(user_id, []):
            if record.billing_method_id != billing_method_id:
                updated_records.append(record)
        self.records[user_id] = updated_records


class FakeIdempotencyKeyRepository:
    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], IdempotencyKey] = {}

    async def find_idempotency_key(
        self,
        scope: str,
        key_hash: str,
    ) -> IdempotencyKey | None:
        return self.keys.get((scope, key_hash))

    async def find_idempotency_key_by_resource(
        self,
        scope: str,
        resource_type: str,
        resource_id: str,
    ) -> IdempotencyKey | None:
        return next(
            (
                key
                for key in self.keys.values()
                if key.scope == scope
                and key.resource_type == resource_type
                and key.resource_id == resource_id
            ),
            None,
        )

    async def find_succeeded_idempotency_key_by_resource(
        self,
        scope: str,
        resource_type: str,
        resource_id: str,
    ) -> IdempotencyKey | None:
        return next(
            (
                key
                for key in self.keys.values()
                if key.scope == scope
                and key.resource_type == resource_type
                and key.resource_id == resource_id
                and key.status == "succeeded"
                and key.response_status == 200
            ),
            None,
        )

    async def save_idempotency_key(self, key: IdempotencyKey) -> None:
        self.keys[(key.scope, key.key_hash)] = key


class FakeOperatorAuditRepository:
    def __init__(self) -> None:
        self.audits: dict[str, OperatorAudit] = {}

    async def save_operator_audit(self, audit: OperatorAudit) -> None:
        self.audits[audit.id] = audit


class FakeBillingMethodDefaultUnitOfWork(BillingMethodDefaultUnitOfWork):
    def __init__(self, factory: FakeBillingMethodDefaultUnitOfWorkFactory) -> None:
        self._factory = factory
        self.billing_methods = factory.billing_methods

    async def __aenter__(self) -> FakeBillingMethodDefaultUnitOfWork:
        self._factory.enter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self._factory.commit_count += 1
        else:
            self._factory.rollback_count += 1


class FakeBillingMethodDefaultUnitOfWorkFactory(
    BillingMethodDefaultUnitOfWorkFactory
):
    def __init__(self, billing_methods: FakeBillingMethodRepository) -> None:
        self.billing_methods = billing_methods
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeBillingMethodDefaultUnitOfWork:
        return FakeBillingMethodDefaultUnitOfWork(self)


def billing_method_record(
    billing_method_id: str,
    *,
    is_default: bool,
    billing_key_status: BillingKeyStatus = "active",
    status: BillingMethodStatus = "active",
) -> BillingMethodRecord:
    return BillingMethodRecord(
        billing_method_id=billing_method_id,
        status=status,
        is_default=is_default,
        method="카드",
        card_company="현대",
        masked_card_number="**** **** **** 1234",
        billing_key_status=billing_key_status,
        created_at=datetime(2026, 6, 8, 10, 15, tzinfo=UTC),
    )


async def test_get_user_billing_methods_requires_user() -> None:
    with pytest.raises(AuthorizationError):
        await get_user_billing_methods(
            requester=RequestContext(request_id="req_1"),
            billing_methods=FakeBillingMethodRepository(),
        )


async def test_get_user_billing_methods_marks_default_not_deletable() -> None:
    repository = FakeBillingMethodRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]
    repository.active_subscription_counts["user_1"] = 2

    result = await get_user_billing_methods(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_methods=repository,
    )

    assert result.default_billing_method_id == "bm_123"
    assert result.active_subscription_count == 2
    assert result.items[0].billing_method_id == "bm_123"
    assert result.items[0].deletable is False
    assert result.items[0].delete_block_reason == "default_method"
    assert result.items[1].billing_method_id == "bm_456"
    assert result.items[1].deletable is True
    assert result.items[1].delete_block_reason is None


async def test_get_user_billing_methods_blocks_last_active_method() -> (
    None
):
    repository = FakeBillingMethodRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=False)
    ]
    repository.active_subscription_counts["user_1"] = 1

    result = await get_user_billing_methods(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_methods=repository,
    )

    assert result.default_billing_method_id is None
    assert result.items[0].deletable is False
    assert (
        result.items[0].delete_block_reason
        == "last_method_for_active_subscriptions"
    )


async def test_set_default_billing_method_requires_user() -> None:
    with pytest.raises(AuthorizationError):
        await set_default_billing_method(
            requester=RequestContext(request_id="req_1"),
            billing_method_id="bm_123",
            billing_methods=FakeBillingMethodRepository(),
            changed_at=datetime(2026, 6, 8, 10, 20, tzinfo=UTC),
        )


async def test_set_default_billing_method_raises_for_missing_method() -> None:
    with pytest.raises(ResourceNotFoundError):
        await set_default_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_missing",
            billing_methods=FakeBillingMethodRepository(),
            changed_at=datetime(2026, 6, 8, 10, 20, tzinfo=UTC),
        )


async def test_set_default_billing_method_forbids_other_user_method() -> None:
    repository = FakeBillingMethodRepository()
    repository.records["user_2"] = [
        billing_method_record("bm_123", is_default=False)
    ]

    with pytest.raises(ForbiddenError):
        await set_default_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_123",
            billing_methods=repository,
            changed_at=datetime(2026, 6, 8, 10, 20, tzinfo=UTC),
        )


async def test_set_default_billing_method_rejects_inactive_billing_key() -> None:
    repository = FakeBillingMethodRepository()
    repository.records["user_1"] = [
        billing_method_record(
            "bm_123",
            is_default=False,
            billing_key_status="revoked",
        )
    ]

    with pytest.raises(InvalidStateTransitionError):
        await set_default_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_123",
            billing_methods=repository,
            changed_at=datetime(2026, 6, 8, 10, 20, tzinfo=UTC),
        )


async def test_set_default_billing_method_changes_user_default() -> None:
    repository = FakeBillingMethodRepository()
    uow_factory = FakeBillingMethodDefaultUnitOfWorkFactory(repository)
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]
    changed_at = datetime(2026, 6, 8, 10, 20, tzinfo=UTC)

    result = await set_default_billing_method(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        changed_at=changed_at,
        billing_method_default_uow_factory=uow_factory,
    )

    assert result.billing_method_id == "bm_456"
    assert result.is_default is True
    assert result.previous_default_billing_method_id == "bm_123"
    assert result.default_changed_at == changed_at
    assert repository.records["user_1"][0].is_default is False
    assert repository.records["user_1"][1].is_default is True
    assert uow_factory.enter_count == 1
    assert uow_factory.commit_count == 1
    assert uow_factory.rollback_count == 0


async def test_set_default_billing_method_translates_stale_update() -> None:
    repository = FakeBillingMethodRepository()
    repository.raise_on_set_default = True
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]

    with pytest.raises(
        InvalidStateTransitionError,
        match="cannot be set as default",
    ):
        await set_default_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_456",
            billing_methods=repository,
            changed_at=datetime(2026, 6, 8, 10, 20, tzinfo=UTC),
        )


async def test_set_default_billing_method_replays_same_idempotency_key() -> None:
    repository = FakeBillingMethodRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]
    changed_at = datetime(2026, 6, 8, 10, 20, tzinfo=UTC)

    first = await set_default_billing_method(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        changed_at=changed_at,
        idempotency_keys=idempotency_keys,
        idempotency_key="default-key",
    )
    second = await set_default_billing_method(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        changed_at=datetime(2026, 6, 8, 10, 21, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="default-key",
    )

    assert second == first
    assert second.previous_default_billing_method_id == "bm_123"


async def test_set_default_billing_method_rejects_idempotency_conflict() -> None:
    repository = FakeBillingMethodRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]

    await set_default_billing_method(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        changed_at=datetime(2026, 6, 8, 10, 20, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="default-key",
    )

    with pytest.raises(IdempotencyConflictError):
        await set_default_billing_method(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            billing_method_id="bm_123",
            billing_methods=repository,
            changed_at=datetime(2026, 6, 8, 10, 21, tzinfo=UTC),
            idempotency_keys=idempotency_keys,
            idempotency_key="default-key",
        )


async def test_set_default_billing_method_is_idempotent_for_existing_default() -> None:
    repository = FakeBillingMethodRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True)
    ]
    changed_at = datetime(2026, 6, 8, 10, 20, tzinfo=UTC)

    result = await set_default_billing_method(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_method_id="bm_123",
        billing_methods=repository,
        changed_at=changed_at,
    )

    assert result.billing_method_id == "bm_123"
    assert result.previous_default_billing_method_id == "bm_123"
    assert result.default_changed_at == changed_at


async def test_delete_billing_method_requires_user() -> None:
    with pytest.raises(AuthorizationError):
        await delete_billing_method(
            requester=RequestContext(request_id="req_1"),
            billing_method_id="bm_123",
            billing_methods=FakeBillingMethodRepository(),
            deleted_at=datetime(2026, 6, 8, 10, 30, tzinfo=UTC),
        )


async def test_delete_billing_method_raises_for_missing_method() -> None:
    with pytest.raises(ResourceNotFoundError):
        await delete_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_missing",
            billing_methods=FakeBillingMethodRepository(),
            deleted_at=datetime(2026, 6, 8, 10, 30, tzinfo=UTC),
        )


async def test_delete_billing_method_forbids_other_user_method() -> None:
    repository = FakeBillingMethodRepository()
    repository.records["user_2"] = [
        billing_method_record("bm_123", is_default=False)
    ]

    with pytest.raises(ForbiddenError):
        await delete_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_123",
            billing_methods=repository,
            deleted_at=datetime(2026, 6, 8, 10, 30, tzinfo=UTC),
        )


async def test_delete_billing_method_rejects_default_method() -> None:
    repository = FakeBillingMethodRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]

    with pytest.raises(InvalidStateTransitionError):
        await delete_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_123",
            billing_methods=repository,
            deleted_at=datetime(2026, 6, 8, 10, 30, tzinfo=UTC),
        )


async def test_delete_billing_method_rejects_last_method_with_active_subscription() -> (
    None
):
    repository = FakeBillingMethodRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=False)
    ]
    repository.active_subscription_counts["user_1"] = 1

    with pytest.raises(InvalidStateTransitionError):
        await delete_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_123",
            billing_methods=repository,
            deleted_at=datetime(2026, 6, 8, 10, 30, tzinfo=UTC),
        )


async def test_delete_billing_method_deactivates_non_default_method() -> None:
    repository = FakeBillingMethodRepository()
    operator_audits = FakeOperatorAuditRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]
    deleted_at = datetime(2026, 6, 8, 10, 30, tzinfo=UTC)

    result = await delete_billing_method(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        deleted_at=deleted_at,
        operator_audits=operator_audits,
    )

    assert result.billing_method_id == "bm_456"
    assert result.status == "inactive"
    assert result.deleted_at == deleted_at
    assert result.remaining_active_method_count == 1
    assert result.default_billing_method_id == "bm_123"
    assert [record.billing_method_id for record in repository.records["user_1"]] == [
        "bm_123"
    ]
    audit = next(iter(operator_audits.audits.values()))
    assert audit.action == "billing_method.delete"
    assert audit.operator_id == "user_1"
    assert audit.target_id == "bm_456"
    assert audit.previous_state["active_method_count"] == 2
    assert audit.previous_state["active_subscription_count"] == 0
    assert audit.next_state["remaining_active_method_count"] == 1
    assert audit.next_state["default_billing_method_id"] == "bm_123"
    assert audit.next_state["billing_key_status"] == "revoked"


async def test_delete_billing_method_rejects_stale_deactivate() -> None:
    repository = FakeBillingMethodRepository()
    operator_audits = FakeOperatorAuditRepository()
    repository.raise_on_deactivate = True
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]

    with pytest.raises(
        InvalidStateTransitionError,
        match="cannot be deleted",
    ):
        await delete_billing_method(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            billing_method_id="bm_456",
            billing_methods=repository,
            deleted_at=datetime(2026, 6, 8, 10, 30, tzinfo=UTC),
            operator_audits=operator_audits,
        )

    assert operator_audits.audits == {}


async def test_delete_billing_method_returns_existing_result_for_inactive_method() -> (
    None
):
    repository = FakeBillingMethodRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record(
            "bm_456",
            is_default=False,
            billing_key_status="revoked",
            status="inactive",
        ),
    ]
    deleted_at = datetime(2026, 6, 8, 10, 30, tzinfo=UTC)

    result = await delete_billing_method(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        deleted_at=deleted_at,
    )

    assert result.billing_method_id == "bm_456"
    assert result.status == "inactive"
    assert result.deleted_at == deleted_at
    assert result.remaining_active_method_count == 1
    assert result.default_billing_method_id == "bm_123"


async def test_delete_billing_method_replays_same_idempotency_key() -> None:
    repository = FakeBillingMethodRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    repository.records["user_1"] = [
        billing_method_record("bm_123", is_default=True),
        billing_method_record("bm_456", is_default=False),
    ]
    deleted_at = datetime(2026, 6, 8, 10, 30, tzinfo=UTC)

    first = await delete_billing_method(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        deleted_at=deleted_at,
        idempotency_keys=idempotency_keys,
        idempotency_key="delete-key",
    )
    second = await delete_billing_method(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        billing_method_id="bm_456",
        billing_methods=repository,
        deleted_at=datetime(2026, 6, 8, 10, 31, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="delete-key",
    )

    assert second == first
