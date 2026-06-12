from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import (
    FakeOperationLockRepository,
    FakeOperatorAuditRepository,
    FakeSubscriptionResumeUnitOfWorkFactory,
)
from payments.application.context import RequestContext
from payments.application.errors import (
    AuthorizationError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
)
from payments.application.subscriptions import (
    DefaultBillingMethodSummary,
    SubscriptionAccountRecord,
    SubscriptionStatus,
    cancel_subscription_at_period_end,
    get_current_user_subscriptions,
    resume_subscription,
)
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.subscription import Subscription


class FakeSubscriptionAccountRepository:
    def __init__(self) -> None:
        self.records: dict[str, list[SubscriptionAccountRecord]] = {}
        self.billing_methods: dict[str, DefaultBillingMethodSummary] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.raise_on_cancel = False
        self.raise_on_resume = False

    async def list_user_subscription_records(
        self,
        user_id: str,
    ) -> list[SubscriptionAccountRecord]:
        return self.records.get(user_id, [])

    async def get_default_billing_method(
        self,
        user_id: str,
    ) -> DefaultBillingMethodSummary | None:
        return self.billing_methods.get(user_id)

    async def get_subscription_for_user(
        self,
        subscription_id: str,
        user_id: str,
    ) -> Subscription | None:
        for subscription in self.subscriptions.values():
            if subscription.id == subscription_id and subscription.user_id == user_id:
                return subscription
        return None

    async def get_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def schedule_subscription_cancel_at_period_end(
        self,
        subscription_id: str,
        user_id: str,
        canceled_at,
    ) -> Subscription:
        if self.raise_on_cancel:
            raise LookupError("subscription was not cancelable")
        subscription = self.subscriptions[subscription_id]
        subscription.status = "cancel_scheduled"
        subscription.cancel_at_period_end = True
        subscription.cancel_at = subscription.current_period_end_at
        subscription.next_billing_at = None
        subscription.access_until = subscription.current_period_end_at
        return subscription

    async def resume_cancel_scheduled_subscription(
        self,
        subscription_id: str,
        user_id: str,
        resumed_at,
    ) -> Subscription:
        if self.raise_on_resume:
            raise LookupError("subscription was not resumable")
        subscription = self.subscriptions[subscription_id]
        subscription.status = "active"
        subscription.cancel_at_period_end = False
        subscription.cancel_at = None
        subscription.next_billing_at = subscription.current_period_end_at
        subscription.access_until = None
        return subscription

    async def save_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription


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


def subscription_record(
    *,
    subscription_id: str,
    status: SubscriptionStatus,
    next_billing_at: datetime | None,
    product_code: str = "basic",
    current_period_end_at: datetime = datetime(2026, 6, 10, tzinfo=UTC),
) -> SubscriptionAccountRecord:
    return SubscriptionAccountRecord(
        subscription_id=subscription_id,
        product_code=product_code,
        plan_id="plan_basic_monthly",
        plan_name="Basic 월간",
        status=status,
        current_period_start_at=datetime(2026, 5, 10, tzinfo=UTC),
        current_period_end_at=current_period_end_at,
        next_billing_at=next_billing_at,
    )


async def test_get_current_user_subscriptions_requires_user() -> None:
    with pytest.raises(AuthorizationError):
        await get_current_user_subscriptions(
            requester=RequestContext(request_id="req_1"),
            subscriptions=FakeSubscriptionAccountRepository(),
        )


async def test_get_current_user_subscriptions_returns_rows_and_default_method() -> None:
    repository = FakeSubscriptionAccountRepository()
    repository.records["user_1"] = [
        subscription_record(
            subscription_id="sub_active",
            status="active",
            next_billing_at=datetime(2026, 6, 10, tzinfo=UTC),
        ),
        subscription_record(
            subscription_id="sub_canceled",
            status="canceled",
            next_billing_at=None,
            product_code="reports",
        ),
    ]
    repository.billing_methods["user_1"] = DefaultBillingMethodSummary(
        billing_method_id="bm_123",
        is_default=True,
        display_name="현대카드 **** 1234",
    )

    account = await get_current_user_subscriptions(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscriptions=repository,
    )

    assert account.billing_method == repository.billing_methods["user_1"]
    assert [subscription.subscription_id for subscription in account.subscriptions] == [
        "sub_active",
        "sub_canceled",
    ]
    assert account.subscriptions[0].resume_available is False
    assert account.subscriptions[0].resubscribe_url is None
    assert account.subscriptions[1].resume_available is False
    assert account.subscriptions[1].resubscribe_url == (
        "/subscriptions/checkout?productCode=reports"
    )


async def test_get_current_user_subscriptions_marks_cancel_scheduled_resumable() -> (
    None
):
    repository = FakeSubscriptionAccountRepository()
    repository.records["user_1"] = [
        subscription_record(
            subscription_id="sub_canceling",
            status="cancel_scheduled",
            next_billing_at=None,
        )
    ]

    account = await get_current_user_subscriptions(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscriptions=repository,
    )

    assert account.subscriptions[0].resume_available is True
    assert account.subscriptions[0].resubscribe_url is None


async def test_get_current_user_subscriptions_returns_one_record_per_product() -> None:
    repository = FakeSubscriptionAccountRepository()
    repository.records["user_1"] = [
        subscription_record(
            subscription_id="sub_basic_canceled_old",
            status="canceled",
            next_billing_at=None,
            current_period_end_at=datetime(2026, 5, 10, tzinfo=UTC),
        ),
        subscription_record(
            subscription_id="sub_reports_canceled_old",
            status="canceled",
            next_billing_at=None,
            product_code="reports",
            current_period_end_at=datetime(2026, 5, 10, tzinfo=UTC),
        ),
        subscription_record(
            subscription_id="sub_basic_active",
            status="active",
            next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
            current_period_end_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
        subscription_record(
            subscription_id="sub_reports_canceled_new",
            status="canceled",
            next_billing_at=None,
            product_code="reports",
            current_period_end_at=datetime(2026, 6, 10, tzinfo=UTC),
        ),
    ]

    account = await get_current_user_subscriptions(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscriptions=repository,
    )

    assert [subscription.subscription_id for subscription in account.subscriptions] == [
        "sub_basic_active",
        "sub_reports_canceled_new",
    ]


def subscription_entity(
    *,
    subscription_id: str = "sub_123",
    user_id: str = "user_1",
    status: SubscriptionStatus = "active",
    current_period_end_at: datetime | None = datetime(2026, 7, 8, tzinfo=UTC),
) -> Subscription:
    return Subscription(
        id=subscription_id,
        user_id=user_id,
        payment_customer_id="pcus_1",
        plan_id="plan_basic_monthly",
        product_code="basic",
        status=status,
        cancel_at_period_end=status == "cancel_scheduled",
        next_billing_at=(
            current_period_end_at if status == "active" else None
        ),
        current_period_start_at=datetime(2026, 6, 8, tzinfo=UTC),
        current_period_end_at=current_period_end_at,
        cancel_at=(
            current_period_end_at if status == "cancel_scheduled" else None
        ),
        access_until=(
            current_period_end_at if status == "cancel_scheduled" else None
        ),
    )


async def test_cancel_subscription_requires_user() -> None:
    with pytest.raises(AuthorizationError):
        await cancel_subscription_at_period_end(
            requester=RequestContext(request_id="req_1"),
            subscription_id="sub_123",
            subscriptions=FakeSubscriptionAccountRepository(),
            canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        )


async def test_cancel_subscription_raises_for_missing_subscription() -> None:
    repository = FakeSubscriptionAccountRepository()

    with pytest.raises(ResourceNotFoundError):
        await cancel_subscription_at_period_end(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            subscription_id="sub_123",
            subscriptions=repository,
            canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        )


async def test_cancel_subscription_raises_for_other_user_subscription() -> None:
    repository = FakeSubscriptionAccountRepository()
    repository.subscriptions = {"sub_123": subscription_entity(user_id="user_2")}

    with pytest.raises(ForbiddenError):
        await cancel_subscription_at_period_end(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            subscription_id="sub_123",
            subscriptions=repository,
            canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        )


async def test_cancel_subscription_schedules_period_end_cancel() -> None:
    repository = FakeSubscriptionAccountRepository()
    operation_locks = FakeOperationLockRepository()
    operator_audits = FakeOperatorAuditRepository()
    repository.subscriptions = {"sub_123": subscription_entity()}

    result = await cancel_subscription_at_period_end(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        cancel_reason="too_expensive",
        feedback="not using next month",
        operation_locks=operation_locks,
        operator_audits=operator_audits,
    )

    assert result.subscription_id == "sub_123"
    assert result.status == "cancel_scheduled"
    assert result.cancel_at == datetime(2026, 7, 8, tzinfo=UTC)
    assert result.next_billing_at is None
    assert result.access_until == datetime(2026, 7, 8, tzinfo=UTC)
    assert result.resume_available is True
    assert operation_locks.acquire_calls == ["subscription:sub_123"]
    assert operation_locks.release_calls == ["subscription:sub_123"]
    audit = next(iter(operator_audits.operator_audits.values()))
    assert audit.action == "subscription.cancel_scheduled"
    assert audit.operator_id == "user_1"
    assert audit.target_id == "sub_123"
    assert audit.previous_state["status"] == "active"
    assert audit.previous_state["next_billing_at"] == "2026-07-08T00:00:00+00:00"
    assert audit.next_state["status"] == "cancel_scheduled"
    assert audit.next_state["next_billing_at"] is None
    assert audit.next_state["cancel_reason"] == "too_expensive"
    assert audit.reason_message == "not using next month"


async def test_cancel_subscription_redacts_sensitive_feedback_from_audit() -> None:
    repository = FakeSubscriptionAccountRepository()
    operator_audits = FakeOperatorAuditRepository()
    repository.subscriptions = {"sub_123": subscription_entity()}

    await cancel_subscription_at_period_end(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        cancel_reason="too_expensive",
        feedback=(
            "email user@example.com phone 010-1234-5678 "
            "account 1234567890123"
        ),
        operator_audits=operator_audits,
    )

    audit = next(iter(operator_audits.operator_audits.values()))
    assert audit.reason_message == (
        "email [redacted] phone [redacted] account [redacted]"
    )
    assert audit.next_state["feedback"] == (
        "email [redacted] phone [redacted] account [redacted]"
    )


async def test_cancel_subscription_replays_same_idempotency_key() -> None:
    repository = FakeSubscriptionAccountRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    repository.subscriptions = {"sub_123": subscription_entity()}

    first = await cancel_subscription_at_period_end(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="cancel-key",
        cancel_reason="too_expensive",
        feedback="not using next month",
    )
    second = await cancel_subscription_at_period_end(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        canceled_at=datetime(2026, 6, 11, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="cancel-key",
        cancel_reason="too_expensive",
        feedback="not using next month",
    )

    assert second == first


async def test_cancel_subscription_rejects_idempotency_conflict() -> None:
    repository = FakeSubscriptionAccountRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    repository.subscriptions = {"sub_123": subscription_entity()}

    await cancel_subscription_at_period_end(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="cancel-key",
        cancel_reason="too_expensive",
        feedback=None,
    )

    with pytest.raises(IdempotencyConflictError):
        await cancel_subscription_at_period_end(
            requester=RequestContext(request_id="req_2", user_id="user_1"),
            subscription_id="sub_123",
            subscriptions=repository,
            canceled_at=datetime(2026, 6, 11, tzinfo=UTC),
            idempotency_keys=idempotency_keys,
            idempotency_key="cancel-key",
            cancel_reason="missing_feature",
            feedback=None,
        )


async def test_cancel_subscription_rejects_canceled_state() -> None:
    repository = FakeSubscriptionAccountRepository()
    repository.subscriptions = {
        "sub_123": subscription_entity(status="canceled")
    }

    with pytest.raises(InvalidStateTransitionError):
        await cancel_subscription_at_period_end(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            subscription_id="sub_123",
            subscriptions=repository,
            canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        )


async def test_cancel_subscription_translates_stale_repository_update() -> None:
    repository = FakeSubscriptionAccountRepository()
    repository.subscriptions = {"sub_123": subscription_entity()}
    repository.raise_on_cancel = True

    with pytest.raises(InvalidStateTransitionError, match="cannot be canceled"):
        await cancel_subscription_at_period_end(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            subscription_id="sub_123",
            subscriptions=repository,
            canceled_at=datetime(2026, 6, 10, tzinfo=UTC),
        )


async def test_resume_subscription_restores_active_billing() -> None:
    repository = FakeSubscriptionAccountRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    operation_locks = FakeOperationLockRepository()
    operator_audits = FakeOperatorAuditRepository()
    uow_factory = FakeSubscriptionResumeUnitOfWorkFactory(
        subscriptions=repository,
        idempotency_keys=idempotency_keys,
        operator_audits=operator_audits,
    )
    repository.subscriptions = {
        "sub_123": subscription_entity(status="cancel_scheduled")
    }

    result = await resume_subscription(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        now=datetime(2026, 6, 10, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="resume-key",
        resume_reason="changed_mind",
        operation_locks=operation_locks,
        operator_audits=operator_audits,
        subscription_resume_uow_factory=uow_factory,
    )

    assert result.subscription_id == "sub_123"
    assert result.status == "active"
    assert result.cancel_at is None
    assert result.next_billing_at == datetime(2026, 7, 8, tzinfo=UTC)
    assert result.access_until is None
    assert result.resume_available is False
    assert operation_locks.acquire_calls == ["subscription:sub_123"]
    assert operation_locks.release_calls == ["subscription:sub_123"]
    assert uow_factory.enter_count == 1
    assert uow_factory.commit_count == 1
    audit = next(iter(operator_audits.operator_audits.values()))
    assert audit.action == "subscription.resume"
    assert audit.operator_id == "user_1"
    assert audit.target_id == "sub_123"
    assert audit.previous_state["status"] == "cancel_scheduled"
    assert audit.previous_state["cancel_at"] == "2026-07-08T00:00:00+00:00"
    assert audit.next_state["status"] == "active"
    assert audit.next_state["cancel_at"] is None
    assert audit.next_state["next_billing_at"] == "2026-07-08T00:00:00+00:00"
    assert audit.next_state["resume_reason"] == "changed_mind"
    assert audit.reason_message == "changed_mind"


async def test_resume_subscription_replays_same_idempotency_key() -> None:
    repository = FakeSubscriptionAccountRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    repository.subscriptions = {
        "sub_123": subscription_entity(status="cancel_scheduled")
    }

    first = await resume_subscription(
        requester=RequestContext(request_id="req_1", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        now=datetime(2026, 6, 10, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="resume-key",
        resume_reason="changed_mind",
    )
    second = await resume_subscription(
        requester=RequestContext(request_id="req_2", user_id="user_1"),
        subscription_id="sub_123",
        subscriptions=repository,
        now=datetime(2026, 6, 11, tzinfo=UTC),
        idempotency_keys=idempotency_keys,
        idempotency_key="resume-key",
        resume_reason="changed_mind",
    )

    assert second == first


async def test_resume_subscription_rejects_expired_period() -> None:
    repository = FakeSubscriptionAccountRepository()
    repository.subscriptions = {
        "sub_123": subscription_entity(status="cancel_scheduled")
    }

    with pytest.raises(InvalidStateTransitionError):
        await resume_subscription(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            subscription_id="sub_123",
            subscriptions=repository,
            now=datetime(2026, 7, 9, tzinfo=UTC),
        )


async def test_resume_subscription_translates_stale_repository_update() -> None:
    repository = FakeSubscriptionAccountRepository()
    repository.subscriptions = {
        "sub_123": subscription_entity(status="cancel_scheduled")
    }
    repository.raise_on_resume = True

    with pytest.raises(InvalidStateTransitionError, match="cannot be resumed"):
        await resume_subscription(
            requester=RequestContext(request_id="req_1", user_id="user_1"),
            subscription_id="sub_123",
            subscriptions=repository,
            now=datetime(2026, 6, 10, tzinfo=UTC),
        )
