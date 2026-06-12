from __future__ import annotations

from datetime import UTC, datetime

import pytest

from payments.adapters.crypto import FernetBillingKeyCipher
from payments.application.billing_auth import (
    BillingAuthIssueCommand,
    BillingAuthStartCommand,
    issue_billing_key,
    start_billing_auth,
)
from payments.application.context import RequestContext
from payments.application.errors import (
    BadRequestError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentRequiredResponseError,
    ProviderError,
)
from payments.application.ports.idempotency import IdempotencyKeyRepository
from payments.application.ports.payment_customers import PaymentCustomerRepository
from payments.application.ports.provider import (
    BillingKeyIssueProviderResult,
    PaymentLookupProviderResult,
)
from payments.domain.entities.billing_auth import BillingAuth
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.payment_customer import PaymentCustomer


class FakeBillingAuthRepository:
    def __init__(self) -> None:
        self.customer_keys: dict[str, str] = {}
        self.active_method_counts: dict[str, int] = {}
        self.auths: dict[str, BillingAuth] = {}
        self.instruments = []
        self.methods = []
        self.default_cleared_for: str | None = None
        self.saved_auths = []

    async def get_customer_key_for_user(self, user_id: str) -> str | None:
        return self.customer_keys.get(user_id)

    async def save_customer_key_for_user(self, user_id: str, customer_key: str) -> None:
        self.customer_keys[user_id] = customer_key

    async def count_active_billing_methods_for_user(self, user_id: str) -> int:
        return self.active_method_counts.get(user_id, 0)

    async def save_billing_auth(self, billing_auth) -> None:
        self.auths[billing_auth.id] = billing_auth
        self.saved_auths.append(billing_auth)

    async def get_billing_auth_for_user(
        self,
        billing_auth_id: str,
        user_id: str,
    ) -> BillingAuth | None:
        auth = self.auths.get(billing_auth_id)
        if auth is None or auth.user_id != user_id:
            return None
        return auth

    async def clear_default_billing_methods_for_user(self, user_id: str) -> None:
        self.default_cleared_for = user_id

    async def save_payment_instrument(self, instrument) -> None:
        self.instruments.append(instrument)

    async def save_billing_method(self, billing_method) -> None:
        self.methods.append(billing_method)


class FakeBillingKeyProvider:
    def __init__(self) -> None:
        self.issue_billing_key_call_count = 0
        self.issue_billing_key_error: ProviderError | None = None

    async def confirm_payment(
        self,
        *,
        payment_key: str,
        order_id: str,
        amount: int,
        idempotency_key: str | None = None,
    ):
        _ = idempotency_key
        raise NotImplementedError

    async def cancel_payment(
        self,
        *,
        payment_key: str,
        cancel_amount: int,
        cancel_reason: str,
        refund_bank_account: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ):
        _ = refund_bank_account, idempotency_key
        raise NotImplementedError

    async def get_payment(
        self,
        *,
        payment_key: str,
    ) -> PaymentLookupProviderResult:
        raise NotImplementedError

    async def issue_billing_key(
        self,
        *,
        auth_key: str,
        customer_key: str,
    ) -> BillingKeyIssueProviderResult:
        self.issue_billing_key_call_count += 1
        if self.issue_billing_key_error is not None:
            raise self.issue_billing_key_error
        return BillingKeyIssueProviderResult(
            billing_key="billing_key_secret",
            method="카드",
            card_company="현대",
            masked_card_number="**** **** **** 1234",
            response_summary={"provider": "tosspayments"},
        )

    async def charge_billing_key(
        self,
        *,
        billing_key: str,
        customer_key: str,
        order_id: str,
        amount: int,
        order_name: str,
        idempotency_key: str | None = None,
    ):
        _ = idempotency_key
        raise NotImplementedError


class FakePaymentCustomerRepository(PaymentCustomerRepository):
    def __init__(self) -> None:
        self.payment_customers: dict[str, PaymentCustomer] = {}

    async def get_active_payment_customer_for_user(
        self,
        user_id: str,
    ) -> PaymentCustomer | None:
        return next(
            (
                customer
                for customer in self.payment_customers.values()
                if customer.user_id == user_id
                and customer.provider == "tosspayments"
                and customer.status == "active"
            ),
            None,
        )

    async def save_payment_customer(self, payment_customer: PaymentCustomer) -> None:
        self.payment_customers[payment_customer.id] = payment_customer


class FakeIdempotencyKeyRepository(IdempotencyKeyRepository):
    def __init__(self) -> None:
        self.idempotency_keys: dict[tuple[str, str], IdempotencyKey] = {}

    async def find_idempotency_key(
        self,
        scope: str,
        key_hash: str,
    ) -> IdempotencyKey | None:
        return self.idempotency_keys.get((scope, key_hash))

    async def find_idempotency_key_by_resource(
        self,
        scope: str,
        resource_type: str,
        resource_id: str,
    ) -> IdempotencyKey | None:
        return next(
            (
                key
                for key in self.idempotency_keys.values()
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
                for key in self.idempotency_keys.values()
                if key.scope == scope
                and key.resource_type == resource_type
                and key.resource_id == resource_id
                and key.status == "succeeded"
                and key.response_status == 200
            ),
            None,
        )

    async def save_idempotency_key(self, key: IdempotencyKey) -> None:
        self.idempotency_keys[(key.scope, key.key_hash)] = key


async def test_start_billing_auth_creates_ready_attempt_and_urls(fixed_clock) -> None:
    repository = FakeBillingAuthRepository()
    payment_customers = FakePaymentCustomerRepository()

    result = await start_billing_auth(
        RequestContext(request_id="req_1", user_id="user_1"),
        BillingAuthStartCommand(
            success_url="https://example.com/success",
            fail_url="https://example.com/fail",
            set_as_default=False,
        ),
        repository,
        payment_customers,
        FakeIdempotencyKeyRepository(),
        fixed_clock,
        client_key="test_ck_local",
    )

    assert result.billing_auth_id.startswith("bauth_")
    assert result.customer_key.startswith("pcus_key_")
    assert result.client_key == "test_ck_local"
    assert result.success_url.endswith(f"billingAuthId={result.billing_auth_id}")
    assert result.fail_url.endswith(f"billingAuthId={result.billing_auth_id}")
    assert result.set_as_default is True
    assert result.status == "ready"
    assert repository.saved_auths[0].status == "ready"
    assert repository.saved_auths[0].payment_customer_id.startswith("pcus_")


async def test_start_billing_auth_rejects_unallowed_redirect_host(
    fixed_clock,
) -> None:
    repository = FakeBillingAuthRepository()
    payment_customers = FakePaymentCustomerRepository()

    with pytest.raises(BadRequestError):
        await start_billing_auth(
            RequestContext(request_id="req_1", user_id="user_1"),
            BillingAuthStartCommand(
                success_url="https://evil.example.net/success",
                fail_url="https://example.com/fail",
                set_as_default=False,
            ),
            repository,
            payment_customers,
            FakeIdempotencyKeyRepository(),
            fixed_clock,
            client_key="test_ck_local",
            allowed_redirect_hosts=("example.com",),
        )

    assert repository.auths == {}
    assert payment_customers.payment_customers == {}


async def test_start_billing_auth_scopes_idempotency_by_user(
    fixed_clock,
) -> None:
    repository = FakeBillingAuthRepository()
    payment_customers = FakePaymentCustomerRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    command = BillingAuthStartCommand(
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        set_as_default=False,
    )

    await start_billing_auth(
        RequestContext(request_id="req_1", user_id="user_1"),
        command,
        repository,
        payment_customers,
        idempotency_keys,
        fixed_clock,
        client_key="test_ck_local",
        idempotency_key="billing-auth-key",
    )

    with pytest.raises(IdempotencyConflictError):
        await start_billing_auth(
            RequestContext(request_id="req_2", user_id="user_2"),
            command,
            repository,
            payment_customers,
            idempotency_keys,
            fixed_clock,
            client_key="test_ck_local",
            idempotency_key="billing-auth-key",
        )


async def test_issue_billing_key_saves_default_billing_method(fixed_clock) -> None:
    repository = FakeBillingAuthRepository()
    payment_customers = FakePaymentCustomerRepository()
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    payment_customers.payment_customers[payment_customer.id] = payment_customer
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=True,
        status="ready",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
    )
    repository.auths[billing_auth.id] = billing_auth
    billing_key_cipher = FernetBillingKeyCipher("test-billing-key-secret")

    result = await issue_billing_key(
        RequestContext(request_id="req_1", user_id="user_1"),
        BillingAuthIssueCommand(
            billing_auth_id=billing_auth.id,
            auth_key="auth_123",
            customer_key=payment_customer.customer_key,
        ),
        repository,
        payment_customers,
        FakeIdempotencyKeyRepository(),
        FakeBillingKeyProvider(),
        fixed_clock,
        billing_key_cipher,
        idempotency_key="billing-issue-key",
    )

    assert result.billing_method_id.startswith("bm_")
    assert result.status == "active"
    assert result.is_default is True
    assert result.method == "카드"
    assert result.card_company == "현대"
    assert result.masked_card_number == "**** **** **** 1234"
    assert result.billing_key_status == "active"
    assert repository.auths[billing_auth.id].status == "issued"
    assert repository.instruments[0].billing_key != "billing_key_secret"
    assert (
        billing_key_cipher.decrypt(repository.instruments[0].billing_key)
        == "billing_key_secret"
    )
    assert repository.methods[0].instrument_id == repository.instruments[0].id
    assert repository.default_cleared_for == "user_1"


async def test_issue_billing_key_marks_auth_failed_on_provider_error(
    fixed_clock,
) -> None:
    repository = FakeBillingAuthRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    payment_customers = FakePaymentCustomerRepository()
    provider = FakeBillingKeyProvider()
    provider.issue_billing_key_error = ProviderError(
        "인증 시간이 만료되었습니다.",
        provider_code="INVALID_AUTH_KEY",
    )
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    payment_customers.payment_customers[payment_customer.id] = payment_customer
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=True,
        status="ready",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
    )
    repository.auths[billing_auth.id] = billing_auth

    with pytest.raises(PaymentRequiredResponseError) as exc_info:
        await issue_billing_key(
            RequestContext(request_id="req_1", user_id="user_1"),
            BillingAuthIssueCommand(
                billing_auth_id=billing_auth.id,
                auth_key="auth_123",
                customer_key=payment_customer.customer_key,
            ),
            repository,
            payment_customers,
            idempotency_keys,
            provider,
            fixed_clock,
            FernetBillingKeyCipher("test-billing-key-secret"),
            idempotency_key="billing-issue-key",
        )

    saved_key = next(iter(idempotency_keys.idempotency_keys.values()))
    assert repository.auths[billing_auth.id].status == "failed"
    assert repository.auths[billing_auth.id].failure == {
        "code": "BILLING_KEY_ISSUE_FAILED",
        "providerCode": "INVALID_AUTH_KEY",
        "message": "인증 시간이 만료되었습니다.",
        "retryable": True,
    }
    assert repository.instruments == []
    assert repository.methods == []
    assert saved_key.status == "failed"
    assert saved_key.response_status == 402
    assert exc_info.value.response_body == {
        "billingAuthId": billing_auth.id,
        "status": "failed",
        "failure": repository.auths[billing_auth.id].failure,
    }

    with pytest.raises(PaymentRequiredResponseError):
        await issue_billing_key(
            RequestContext(request_id="req_2", user_id="user_1"),
            BillingAuthIssueCommand(
                billing_auth_id=billing_auth.id,
                auth_key="auth_123",
                customer_key=payment_customer.customer_key,
            ),
            repository,
            payment_customers,
            idempotency_keys,
            provider,
            fixed_clock,
            FernetBillingKeyCipher("test-billing-key-secret"),
            idempotency_key="billing-issue-key",
        )
    assert provider.issue_billing_key_call_count == 1


async def test_issue_billing_key_marks_auth_expired_before_provider_call(
    fixed_clock,
) -> None:
    repository = FakeBillingAuthRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    payment_customers = FakePaymentCustomerRepository()
    provider = FakeBillingKeyProvider()
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    payment_customers.payment_customers[payment_customer.id] = payment_customer
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=True,
        status="ready",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
        expires_at=datetime(2026, 6, 9, 0, 30, tzinfo=UTC),
    )
    repository.auths[billing_auth.id] = billing_auth

    with pytest.raises(InvalidStateTransitionError):
        await issue_billing_key(
            RequestContext(request_id="req_1", user_id="user_1"),
            BillingAuthIssueCommand(
                billing_auth_id=billing_auth.id,
                auth_key="auth_123",
                customer_key=payment_customer.customer_key,
            ),
            repository,
            payment_customers,
            idempotency_keys,
            provider,
            fixed_clock,
            FernetBillingKeyCipher("test-billing-key-secret"),
            idempotency_key="billing-issue-key",
        )

    assert repository.auths[billing_auth.id].status == "expired"
    assert provider.issue_billing_key_call_count == 0
    assert idempotency_keys.idempotency_keys == {}
    assert repository.instruments == []
    assert repository.methods == []


async def test_issue_billing_key_rejects_customer_key_mismatch_as_bad_request(
    fixed_clock,
) -> None:
    repository = FakeBillingAuthRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    payment_customers = FakePaymentCustomerRepository()
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    payment_customers.payment_customers[payment_customer.id] = payment_customer
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=True,
        status="ready",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
    )
    repository.auths[billing_auth.id] = billing_auth
    provider = FakeBillingKeyProvider()

    with pytest.raises(BadRequestError):
        await issue_billing_key(
            RequestContext(request_id="req_1", user_id="user_1"),
            BillingAuthIssueCommand(
                billing_auth_id=billing_auth.id,
                auth_key="auth_123",
                customer_key="pcus_key_other",
            ),
            repository,
            payment_customers,
            idempotency_keys,
            provider,
            fixed_clock,
            FernetBillingKeyCipher("test-billing-key-secret"),
            idempotency_key="billing-issue-key",
        )

    assert billing_auth.status == "ready"
    assert provider.issue_billing_key_call_count == 0
    assert idempotency_keys.idempotency_keys == {}


async def test_issue_billing_key_replays_same_idempotency_key(fixed_clock) -> None:
    repository = FakeBillingAuthRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    payment_customers = FakePaymentCustomerRepository()
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    payment_customers.payment_customers[payment_customer.id] = payment_customer
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=True,
        status="ready",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
    )
    repository.auths[billing_auth.id] = billing_auth
    provider = FakeBillingKeyProvider()
    kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "command": BillingAuthIssueCommand(
            billing_auth_id=billing_auth.id,
            auth_key="auth_123",
            customer_key=payment_customer.customer_key,
        ),
        "repository": repository,
        "payment_customers": payment_customers,
        "idempotency_keys": idempotency_keys,
        "provider": provider,
        "clock": fixed_clock,
        "billing_key_cipher": FernetBillingKeyCipher("test-billing-key-secret"),
        "idempotency_key": "billing-issue-key",
    }

    first = await issue_billing_key(**kwargs)
    second = await issue_billing_key(**kwargs)

    assert second == first
    assert provider.issue_billing_key_call_count == 1
    assert len(repository.instruments) == 1
    assert len(repository.methods) == 1


async def test_issue_billing_key_replays_same_billing_auth_with_new_key(
    fixed_clock,
) -> None:
    repository = FakeBillingAuthRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    payment_customers = FakePaymentCustomerRepository()
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    payment_customers.payment_customers[payment_customer.id] = payment_customer
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=True,
        status="ready",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
    )
    repository.auths[billing_auth.id] = billing_auth
    provider = FakeBillingKeyProvider()
    base_kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "command": BillingAuthIssueCommand(
            billing_auth_id=billing_auth.id,
            auth_key="auth_123",
            customer_key=payment_customer.customer_key,
        ),
        "repository": repository,
        "payment_customers": payment_customers,
        "idempotency_keys": idempotency_keys,
        "provider": provider,
        "clock": fixed_clock,
        "billing_key_cipher": FernetBillingKeyCipher("test-billing-key-secret"),
    }

    first = await issue_billing_key(
        **base_kwargs,
        idempotency_key="billing-issue-key-1",
    )
    second = await issue_billing_key(
        **{
            **base_kwargs,
            "requester": RequestContext(request_id="req_2", user_id="user_1"),
        },
        idempotency_key="billing-issue-key-2",
    )

    assert second == first
    assert provider.issue_billing_key_call_count == 1
    assert len(repository.instruments) == 1
    assert len(repository.methods) == 1


async def test_issue_billing_key_rejects_idempotency_conflict(fixed_clock) -> None:
    repository = FakeBillingAuthRepository()
    idempotency_keys = FakeIdempotencyKeyRepository()
    payment_customers = FakePaymentCustomerRepository()
    payment_customer = PaymentCustomer(
        id="pcus_1",
        user_id="user_1",
        provider="tosspayments",
        customer_key="pcus_key_1",
        status="active",
    )
    payment_customers.payment_customers[payment_customer.id] = payment_customer
    billing_auth = BillingAuth(
        id="bauth_123",
        user_id="user_1",
        payment_customer_id=payment_customer.id,
        customer_key_snapshot=payment_customer.customer_key,
        set_as_default=True,
        status="ready",
        success_url="https://example.com/success",
        fail_url="https://example.com/fail",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
        expires_at=datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
    )
    repository.auths[billing_auth.id] = billing_auth
    kwargs = {
        "requester": RequestContext(request_id="req_1", user_id="user_1"),
        "command": BillingAuthIssueCommand(
            billing_auth_id=billing_auth.id,
            auth_key="auth_123",
            customer_key=payment_customer.customer_key,
        ),
        "repository": repository,
        "payment_customers": payment_customers,
        "idempotency_keys": idempotency_keys,
        "provider": FakeBillingKeyProvider(),
        "clock": fixed_clock,
        "billing_key_cipher": FernetBillingKeyCipher("test-billing-key-secret"),
        "idempotency_key": "billing-issue-key",
    }
    await issue_billing_key(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        await issue_billing_key(
            **{
                **kwargs,
                "command": BillingAuthIssueCommand(
                    billing_auth_id=billing_auth.id,
                    auth_key="auth_other",
                    customer_key=payment_customer.customer_key,
                ),
            }
        )
