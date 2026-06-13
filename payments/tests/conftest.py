from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import copy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import TracebackType

import pytest
from fastapi.testclient import TestClient

from payments.adapters.crypto import FernetBillingKeyCipher
from payments.adapters.subscription_change_tokens import (
    HmacSubscriptionChangeTokenCodec,
)
from payments.application.admin_auth import _sign_access_token
from payments.application.cursors import decode_cursor
from payments.application.errors import ProviderError
from payments.application.notifications import NotificationEnqueueDependencies
from payments.application.ports import (
    AdminAuthRateLimiter,
    AdminAuthUnitOfWork,
    AdminAuthUnitOfWorkFactory,
    AdminCatalogRepository,
    AdminListQuery,
    AdminPaymentListRecord,
    AdminProductListRecord,
    AdminProductQuery,
    AdminSubscriptionAdjustUnitOfWork,
    AdminSubscriptionAdjustUnitOfWorkFactory,
    AdminSubscriptionListRecord,
    BillingAuthIssueUnitOfWork,
    BillingAuthIssueUnitOfWorkFactory,
    BillingChargeProviderResult,
    BillingKeyCipher,
    BillingKeyIssueProviderResult,
    BillingMethodDefaultUnitOfWork,
    BillingMethodDefaultUnitOfWorkFactory,
    BillingMethodDeleteUnitOfWork,
    BillingMethodDeleteUnitOfWorkFactory,
    BillingMethodRecord,
    BillingRetryRepository,
    CheckoutRepository,
    DefaultBillingMethodSummary,
    IdempotencyKeyRepository,
    InvoiceDetailRecord,
    InvoiceListRecord,
    InvoiceWriteRepository,
    OneTimePaymentUnitOfWork,
    OneTimePaymentUnitOfWorkFactory,
    OneTimeSkuRepository,
    OperationLockRepository,
    OperatorAuditQuery,
    OperatorAuditRepository,
    PaymentAttemptRepository,
    PaymentCancelProviderResult,
    PaymentCancelRequestRepository,
    PaymentConfirmProviderResult,
    PaymentCustomerRepository,
    PaymentLookupProviderResult,
    PaymentProvider,
    ResolvedNotificationRecipient,
    SchedulerRunLogRepository,
    SchedulerRunQuery,
    SubscriptionAccountRecord,
    SubscriptionAccountRepository,
    SubscriptionBillingUnitOfWork,
    SubscriptionBillingUnitOfWorkFactory,
    SubscriptionCancelUnitOfWork,
    SubscriptionCancelUnitOfWorkFactory,
    SubscriptionChangeUnitOfWork,
    SubscriptionChangeUnitOfWorkFactory,
    SubscriptionConfirmUnitOfWork,
    SubscriptionConfirmUnitOfWorkFactory,
    SubscriptionExpirationUnitOfWork,
    SubscriptionExpirationUnitOfWorkFactory,
    SubscriptionResumeUnitOfWork,
    SubscriptionResumeUnitOfWorkFactory,
    WebhookUnitOfWork,
    WebhookUnitOfWorkFactory,
)
from payments.domain.entities.admin_auth import AdminAccount, AdminAuthToken
from payments.domain.entities.billing_auth import BillingAuth
from payments.domain.entities.billing_method import BillingMethod
from payments.domain.entities.checkout import Checkout
from payments.domain.entities.idempotency_key import IdempotencyKey
from payments.domain.entities.invoice import Invoice
from payments.domain.entities.notification import (
    NotificationLastError,
    NotificationOutboxItem,
    NotificationTemplate,
)
from payments.domain.entities.one_time_sku import OneTimeSku
from payments.domain.entities.operation_lock import OperationLock
from payments.domain.entities.operator_audit import OperatorAudit
from payments.domain.entities.payment import Payment
from payments.domain.entities.payment_cancel_request import PaymentCancelRequest
from payments.domain.entities.payment_customer import PaymentCustomer
from payments.domain.entities.payment_instrument import PaymentInstrument
from payments.domain.entities.product import Product
from payments.domain.entities.scheduler_run import SchedulerRunLog
from payments.domain.entities.subscription import Subscription
from payments.domain.entities.subscription_plan import SubscriptionPlan
from payments.domain.entities.webhook_event import WebhookEvent
from payments.http.composition import create_app
from payments.http.dependencies import HttpDependencies


class FixedClock:
    def utc_now(self) -> datetime:
        return datetime(2026, 6, 10, 0, 0, tzinfo=UTC)


class FakeNotificationOutboxRepository:
    def __init__(self) -> None:
        self.items: dict[str, NotificationOutboxItem] = {}

    async def enqueue_idempotently(
        self,
        item: NotificationOutboxItem,
    ) -> NotificationOutboxItem:
        existing = self.items.get(item.idempotency_key)
        if existing is not None:
            return existing
        self.items[item.idempotency_key] = item
        return item

    async def claim_due_notifications(
        self,
        *,
        now: datetime,
        lock_until: datetime,
        worker_id: str,
        limit: int,
    ) -> list[NotificationOutboxItem]:
        _ = now, lock_until, worker_id, limit
        return []

    async def mark_sent(
        self,
        item_id: str,
        *,
        provider_message_id: str,
        sent_at: datetime,
        purge_after_at: datetime,
    ) -> None:
        _ = item_id, provider_message_id, sent_at, purge_after_at

    async def schedule_retry(
        self,
        item_id: str,
        *,
        available_at: datetime,
        last_error: NotificationLastError,
    ) -> None:
        _ = item_id, available_at, last_error

    async def mark_dead_letter(
        self,
        item_id: str,
        *,
        last_error: NotificationLastError,
        purge_after_at: datetime,
    ) -> None:
        _ = item_id, last_error, purge_after_at


class FakeNotificationTemplateRepository:
    async def resolve_active_template(
        self,
        *,
        event_type: str,
        product_code: str | None,
        product_type: str | None,
    ) -> NotificationTemplate | None:
        _ = product_code, product_type
        return NotificationTemplate(
            id=f"ntpl_{event_type}",
            template_key=f"default.{event_type}",
            version=1,
            event_type=event_type,
            product_code=None,
            product_type=None,
            status="active",
            subject_template="{{ recipientName|default('고객님') }}",
            html_template="{{ recipientName|default('고객님') }}",
            text_template="{{ recipientName|default('고객님') }}",
            required_template_args=[],
            created_at=FixedClock().utc_now(),
            updated_at=FixedClock().utc_now(),
        )

    async def get_template(
        self,
        *,
        template_key: str,
        version: int,
    ) -> NotificationTemplate | None:
        event_type = template_key.removeprefix("default.")
        return await self.resolve_active_template(
            event_type=event_type,
            product_code=None,
            product_type=None,
        )

    async def count_templates(self) -> int:
        return 1

    async def save_template(self, template: NotificationTemplate) -> None:
        _ = template


class FakeNotificationRecipientResolver:
    async def resolve_user(self, user_id: str) -> ResolvedNotificationRecipient:
        return ResolvedNotificationRecipient(
            recipient_type="user",
            email=f"{user_id}@example.com",
            recipient_name="Test User",
            recipient_user_id=user_id,
            recipient_admin_id=None,
        )

    async def resolve_admin(self, admin_id: str) -> ResolvedNotificationRecipient:
        return ResolvedNotificationRecipient(
            recipient_type="admin",
            email=f"{admin_id}@example.com",
            recipient_name="Test Admin",
            recipient_user_id=None,
            recipient_admin_id=admin_id,
        )


class FakeTemplateArgCipher:
    def encrypt(self, plaintext: str) -> str:
        return f"encrypted:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("encrypted:")


def fake_notification_enqueue_dependencies() -> NotificationEnqueueDependencies:
    return NotificationEnqueueDependencies(
        outbox_repository=FakeNotificationOutboxRepository(),
        template_repository=FakeNotificationTemplateRepository(),
        recipient_resolver=FakeNotificationRecipientResolver(),
        template_arg_cipher=FakeTemplateArgCipher(),
        clock=FixedClock(),
    )


class FakeCatalogRepository:
    def __init__(self) -> None:
        self.product = Product(
            id="product_basic",
            product_code="basic",
            product_type="subscription",
            name="Basic",
            status="active",
        )
        self.plan = SubscriptionPlan(
            id="plan_basic_monthly",
            product_id="product_basic",
            plan_code="basic_monthly",
            billing_period="monthly",
            amount=9900,
            entitlements={"seats": 1},
            status="active",
        )
        self.products: dict[str, Product] = {self.product.id: self.product}
        self.plans: dict[str, SubscriptionPlan] = {self.plan.id: self.plan}
        self.subscriptions: dict[str, Subscription] = {}

    async def list_active_subscription_catalog(self):
        rows = []
        for plan in self.plans.values():
            product = self.products.get(plan.product_id)
            if (
                product is not None
                and product.status == "active"
                and product.product_type == "subscription"
                and plan.status == "active"
            ):
                rows.append((product, plan))
        return rows

    async def get_active_subscription_plan(self, plan_id: str):
        plan = self.plans.get(plan_id)
        if plan is None or plan.status != "active":
            return None
        product = self.products.get(plan.product_id)
        if (
            product is not None
            and product.status == "active"
            and product.product_type == "subscription"
        ):
            return (product, plan)
        return None

    async def list_user_active_product_subscriptions(self, user_id: str):
        return [
            subscription
            for subscription in self.subscriptions.values()
            if subscription.user_id == user_id
            and subscription.status
            in {"pending", "active", "past_due", "cancel_scheduled"}
        ]


class FakeAdminAuthRepository:
    def __init__(self) -> None:
        self.admin_accounts: dict[str, AdminAccount] = {}
        self.auth_tokens: dict[str, AdminAuthToken] = {}

    async def get_admin_by_email_lower(
        self,
        email_lower: str,
    ) -> AdminAccount | None:
        return next(
            (
                admin
                for admin in self.admin_accounts.values()
                if admin.email_lower == email_lower
            ),
            None,
        )

    async def get_admin_account(self, admin_id: str) -> AdminAccount | None:
        return self.admin_accounts.get(admin_id)

    async def save_admin_account(self, admin: AdminAccount) -> None:
        self.admin_accounts[admin.id] = admin

    async def save_auth_token(self, token: AdminAuthToken) -> None:
        self.auth_tokens[token.id] = token

    async def get_auth_token_by_hash(
        self,
        token_hash: str,
    ) -> AdminAuthToken | None:
        return next(
            (
                token
                for token in self.auth_tokens.values()
                if token.token_hash == token_hash
            ),
            None,
        )

    async def revoke_active_refresh_tokens(
        self,
        admin_account_id: str,
        revoked_at,
        request_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        for token in self.auth_tokens.values():
            if (
                token.admin_account_id == admin_account_id
                and token.token_type == "refresh_token"
                and token.status == "active"
            ):
                token.status = "revoked"
                token.consumed_at = revoked_at
                token.last_used_at = revoked_at
                if request_ip is not None:
                    token.request_ip = request_ip
                if user_agent is not None:
                    token.user_agent = user_agent


class FakeAdminAuthEmailSender:
    def __init__(self) -> None:
        self.login_links: list[tuple[str, str]] = []
        self.password_reset_links: list[tuple[str, str]] = []
        self.fail_login_link = False

    async def send_login_link(
        self,
        *,
        admin_id: str,
        email: str,
        recipient_name: str | None,
        auth_token_id: str,
        login_token: str,
        expires_at: datetime,
        request_ip: str | None,
        user_agent: str | None,
    ) -> None:
        _ = (
            admin_id,
            recipient_name,
            auth_token_id,
            expires_at,
            request_ip,
            user_agent,
        )
        if self.fail_login_link:
            raise RuntimeError("SMTP unavailable")
        self.login_links.append((email, login_token))

    async def send_password_reset_link(
        self,
        *,
        admin_id: str,
        email: str,
        recipient_name: str | None,
        auth_token_id: str,
        reset_token: str,
        expires_at: datetime,
        request_ip: str | None,
    ) -> None:
        _ = (admin_id, recipient_name, auth_token_id, expires_at, request_ip)
        self.password_reset_links.append((email, reset_token))


class FakeAdminAuthRateLimiter(AdminAuthRateLimiter):
    def __init__(self) -> None:
        self.attempts: dict[str, list[datetime]] = {}

    async def count_attempts(
        self,
        key: str,
        *,
        since: datetime,
    ) -> int:
        attempts = [
            attempt for attempt in self.attempts.get(key, []) if attempt >= since
        ]
        self.attempts[key] = attempts
        return len(attempts)

    async def record_attempt(
        self,
        key: str,
        *,
        attempted_at: datetime,
        window: timedelta,
    ) -> None:
        since = attempted_at - window
        attempts = [
            attempt for attempt in self.attempts.get(key, []) if attempt >= since
        ]
        attempts.append(attempted_at)
        self.attempts[key] = attempts


class FakeAdminAuthUnitOfWork(AdminAuthUnitOfWork):
    def __init__(self, factory: FakeAdminAuthUnitOfWorkFactory) -> None:
        self._factory = factory
        self.admin_auth = factory.admin_auth

    async def __aenter__(self) -> FakeAdminAuthUnitOfWork:
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


class FakeAdminAuthUnitOfWorkFactory(AdminAuthUnitOfWorkFactory):
    def __init__(self, admin_auth: FakeAdminAuthRepository) -> None:
        self.admin_auth = admin_auth
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeAdminAuthUnitOfWork:
        return FakeAdminAuthUnitOfWork(self)


class FakeAdminCatalogRepository(AdminCatalogRepository):
    def __init__(self) -> None:
        self.products: dict[str, Product] = {}
        self.subscription_plans: dict[str, SubscriptionPlan] = {}
        self.one_time_skus: dict[str, OneTimeSku] = {}
        self.audit_records: list[dict[str, object]] = []
        self.active_subscription_plan_counts: dict[str, int] = {}
        self.active_one_time_sku_counts: dict[str, int] = {}

    async def list_products(
        self,
        query: AdminProductQuery,
    ) -> list[AdminProductListRecord]:
        products = list(self.products.values())
        if query.product_type is not None:
            products = [
                product
                for product in products
                if product.product_type == query.product_type
            ]
        if query.status is not None:
            products = [
                product for product in products if product.status in query.status
            ]
        if query.keyword is not None:
            keyword = query.keyword.casefold()
            products = [
                product
                for product in products
                if keyword in product.product_code.casefold()
                or keyword in product.name.casefold()
            ]
        products = sorted(
            products,
            key=lambda product: (product.product_code, product.id),
        )
        if query.cursor is not None:
            payload = decode_cursor(query.cursor)
            product_code = str(payload["productCode"])
            product_id = str(payload["productId"])
            products = [
                product
                for product in products
                if (product.product_code, product.id) > (product_code, product_id)
            ]
        return [
            AdminProductListRecord(
                product=product,
                subscription_plan_count=sum(
                    1
                    for plan in self.subscription_plans.values()
                    if plan.product_id == product.id
                ),
                active_subscription_plan_count=sum(
                    1
                    for plan in self.subscription_plans.values()
                    if plan.product_id == product.id and plan.status == "active"
                ),
                one_time_sku_count=sum(
                    1
                    for sku in self.one_time_skus.values()
                    if sku.product_id == product.id
                ),
                active_one_time_sku_count=sum(
                    1
                    for sku in self.one_time_skus.values()
                    if sku.product_id == product.id and sku.status == "active"
                ),
            )
            for product in products[: query.limit]
        ]

    async def get_product(self, product_id: str) -> Product | None:
        return self.products.get(product_id)

    async def list_subscription_plans(
        self,
        product_id: str,
    ) -> list[SubscriptionPlan]:
        return [
            plan
            for plan in sorted(
                self.subscription_plans.values(),
                key=lambda item: (item.plan_code, item.id),
            )
            if plan.product_id == product_id
        ]

    async def list_one_time_skus(
        self,
        product_id: str,
    ) -> list[OneTimeSku]:
        return [
            sku
            for sku in sorted(
                self.one_time_skus.values(),
                key=lambda item: (item.sku_code, item.id),
            )
            if sku.product_id == product_id
        ]

    async def list_product_audit_records(
        self,
        product_id: str,
        child_ids: tuple[str, ...],
        limit: int,
    ) -> list[OperatorAudit]:
        target_ids = {product_id, *child_ids}
        audits = [
            OperatorAudit(
                id=str(record.get("request_id", "req")),
                operator_id=str(record["admin_id"]),
                action=str(record["action"]),
                target_type="product",
                target_id=str(record["product_id"]),
                previous_state=_audit_state(record.get("previous")),
                next_state=_audit_state(record.get("next")),
                reason_code=str(record["action"]),
                result="succeeded",
                created_at=_audit_created_at(record.get("created_at")),
            )
            for record in self.audit_records
            if record.get("product_id") in target_ids
        ]
        return sorted(
            audits,
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )[:limit]

    async def get_product_by_code(
        self,
        product_code: str,
        product_type: str,
    ) -> Product | None:
        return next(
            (
                product
                for product in self.products.values()
                if product.product_code == product_code
                and product.product_type == product_type
            ),
            None,
        )

    async def save_product(self, product: Product) -> None:
        self.products[product.id] = product

    async def count_active_subscription_plans(self, product_id: str) -> int:
        return self.active_subscription_plan_counts.get(
            product_id,
            sum(
                1
                for plan in self.subscription_plans.values()
                if plan.product_id == product_id and plan.status == "active"
            ),
        )

    async def count_active_one_time_skus(self, product_id: str) -> int:
        return self.active_one_time_sku_counts.get(
            product_id,
            sum(
                1
                for sku in self.one_time_skus.values()
                if sku.product_id == product_id and sku.status == "active"
            ),
        )

    async def get_subscription_plan(
        self,
        product_id: str,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        plan = self.subscription_plans.get(plan_id)
        if plan is None or plan.product_id != product_id:
            return None
        return plan

    async def get_subscription_plan_by_code(
        self,
        product_id: str,
        plan_code: str,
    ) -> SubscriptionPlan | None:
        return next(
            (
                plan
                for plan in self.subscription_plans.values()
                if plan.product_id == product_id and plan.plan_code == plan_code
            ),
            None,
        )

    async def save_subscription_plan(self, plan: SubscriptionPlan) -> None:
        self.subscription_plans[plan.id] = plan

    async def get_one_time_sku(
        self,
        product_id: str,
        sku_id: str,
    ) -> OneTimeSku | None:
        sku = self.one_time_skus.get(sku_id)
        if sku is None or sku.product_id != product_id:
            return None
        return sku

    async def get_one_time_sku_by_code(
        self,
        product_id: str,
        sku_code: str,
    ) -> OneTimeSku | None:
        return next(
            (
                sku
                for sku in self.one_time_skus.values()
                if sku.product_id == product_id and sku.sku_code == sku_code
            ),
            None,
        )

    async def save_one_time_sku(self, sku: OneTimeSku) -> None:
        self.one_time_skus[sku.id] = sku

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
        self.audit_records.append(
            {
                "product_id": product_id,
                "admin_id": admin_id,
                "request_id": request_id,
                "action": action,
                "previous": previous,
                "next": next_value,
                "request_ip": request_ip,
                "created_at": created_at,
            }
        )


def _audit_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _audit_created_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return FixedClock().utc_now()


class FakeAdminOperationsRepository:
    def __init__(self) -> None:
        self.subscriptions: dict[str, Subscription] = {}
        self.subscription_plans: dict[str, SubscriptionPlan] = {
            "plan_basic_monthly": SubscriptionPlan(
                id="plan_basic_monthly",
                product_id="product_basic",
                plan_code="basic_monthly",
                billing_period="monthly",
                amount=9900,
                entitlements={"seats": 1},
                status="active",
            )
        }
        self.payments: dict[str, Payment] = {}
        self.invoices: dict[str, Invoice] = {}
        self.audit_records: list[dict[str, object]] = []
        self.payment_records = [
            AdminPaymentListRecord(
                payment_id="pay_123",
                checkout_id="chk_123",
                user_id="user_1",
                user_email="customer@example.com",
                order_id="order_123",
                order_name="Report pack",
                payment_key="paykey_123",
                status="paid",
                amount=25000,
                paid_amount=25000,
                cancelable_amount=25000,
                currency="KRW",
                created_at=datetime(2026, 6, 8, 1, 15, tzinfo=UTC),
                approved_at=datetime(2026, 6, 8, 1, 15, tzinfo=UTC),
                method_summary="card 1234",
            )
        ]
        self.subscription_records = [
            AdminSubscriptionListRecord(
                subscription_id="sub_123",
                user_id="user_1",
                user_email="customer@example.com",
                product_code="analytics",
                product_name="Analytics",
                plan_id="plan_basic",
                plan_name="Basic monthly",
                status="past_due",
                current_period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
                current_period_end_at=datetime(2026, 6, 30, tzinfo=UTC),
                next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
                payment_failure={
                    "hasFailure": True,
                    "lastInvoiceId": "inv_123",
                    "failureCode": "CARD_DECLINED",
                    "retryScheduledAt": None,
                },
                default_billing_method_summary="card 1234",
            )
        ]

    async def list_admin_payments(
        self,
        query: AdminListQuery,
    ) -> list[AdminPaymentListRecord]:
        records = self.payment_records
        if query.status is not None:
            status_values = (
                (query.status,) if isinstance(query.status, str) else query.status
            )
            records = [
                record for record in records if record.status in status_values
            ]
        if query.user_id is not None:
            records = [record for record in records if record.user_id == query.user_id]
        if query.order_id is not None:
            records = [
                record for record in records if record.order_id == query.order_id
            ]
        if query.payment_key is not None:
            records = [
                record for record in records if record.payment_key == query.payment_key
            ]
        if query.from_at is not None:
            records = [
                record
                for record in records
                if (record.approved_at or record.created_at) >= query.from_at
            ]
        if query.to_at is not None:
            records = [
                record
                for record in records
                if (record.approved_at or record.created_at) <= query.to_at
            ]
        records = sorted(
            records,
            key=lambda record: (
                record.approved_at or record.created_at,
                record.payment_id,
            ),
            reverse=True,
        )
        if query.cursor is not None:
            payload = decode_cursor(query.cursor)
            sort_at = datetime.fromisoformat(
                str(payload.get("sortAt") or payload["approvedAt"]).replace(
                    "Z",
                    "+00:00",
                )
            )
            payment_id = str(payload["paymentId"])
            records = [
                record
                for record in records
                if (
                    (record.approved_at or record.created_at) < sort_at
                    or (
                        (record.approved_at or record.created_at) == sort_at
                        and record.payment_id < payment_id
                    )
                )
            ]
        return records[: query.limit]

    async def list_admin_subscriptions(
        self,
        query: AdminListQuery,
    ) -> list[AdminSubscriptionListRecord]:
        records = self.subscription_records
        if query.status is not None:
            status_values = (
                (query.status,) if isinstance(query.status, str) else query.status
            )
            records = [
                record for record in records if record.status in status_values
            ]
        if query.user_id is not None:
            records = [record for record in records if record.user_id == query.user_id]
        if query.product_code is not None:
            records = [
                record
                for record in records
                if record.product_code == query.product_code
            ]
        if query.payment_failure is not None:
            records = [
                record
                for record in records
                if bool(
                    record.payment_failure
                    and record.payment_failure.get("hasFailure")
                )
                is query.payment_failure
            ]
        if query.next_billing_from is not None:
            records = [
                record
                for record in records
                if record.next_billing_at is not None
                and record.next_billing_at >= query.next_billing_from
            ]
        if query.next_billing_to is not None:
            records = [
                record
                for record in records
                if record.next_billing_at is not None
                and record.next_billing_at <= query.next_billing_to
            ]
        records = sorted(
            records,
            key=lambda record: (
                record.next_billing_at or datetime.max.replace(tzinfo=UTC),
                record.subscription_id,
            ),
        )
        if query.cursor is not None:
            payload = decode_cursor(query.cursor)
            next_billing_value = payload.get("nextBillingAt")
            next_billing_at = (
                None
                if next_billing_value is None
                and payload.get("nextBillingAtNull") is True
                else datetime.fromisoformat(
                    str(next_billing_value).replace("Z", "+00:00")
                )
            )
            subscription_id = str(payload["subscriptionId"])
            cursor_key = (
                next_billing_at is None,
                next_billing_at or datetime.max.replace(tzinfo=UTC),
                subscription_id,
            )
            records = [
                record
                for record in records
                if (
                    record.next_billing_at is None,
                    record.next_billing_at or datetime.max.replace(tzinfo=UTC),
                    record.subscription_id,
                )
                > cursor_key
            ]
        return records[: query.limit]

    async def save_admin_list_audit_record(
        self,
        *,
        audit_id: str,
        admin_id: str,
        request_id: str,
        action: str,
        target_type: str,
        target_id: str,
        query: dict[str, object],
        result_count: int,
        has_more: bool,
        request_ip: str | None,
        created_at: datetime,
    ) -> None:
        self.audit_records.append(
            {
                "audit_id": audit_id,
                "admin_id": admin_id,
                "request_id": request_id,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "query": query,
                "result_count": result_count,
                "has_more": has_more,
                "request_ip": request_ip,
                "created_at": created_at,
            }
        )

    async def get_admin_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def get_admin_subscription_plan(
        self,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        return self.subscription_plans.get(plan_id)

    async def get_admin_payment_by_invoice_id(
        self,
        invoice_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        invoice = self.invoices.get(invoice_id)
        if invoice is None:
            return (None, None)
        return (self.payments.get(invoice.payment_id), invoice)

    async def get_admin_payment_by_payment_key(
        self,
        payment_key: str,
    ) -> Payment | None:
        return next(
            (
                payment
                for payment in self.payments.values()
                if payment.payment_key == payment_key
            ),
            None,
        )

    async def get_admin_invoice_by_payment_id(
        self,
        payment_id: str,
    ) -> Invoice | None:
        return next(
            (
                invoice
                for invoice in self.invoices.values()
                if invoice.payment_id == payment_id
            ),
            None,
        )

    async def get_admin_latest_failed_subscription_payment(
        self,
        subscription_id: str,
    ) -> tuple[Payment | None, Invoice | None]:
        payments = [
            payment
            for payment in self.payments.values()
            if payment.subscription_id == subscription_id and payment.status == "failed"
        ]
        if not payments:
            return (None, None)
        payment = max(payments, key=lambda item: item.created_at)
        return (payment, await self.get_admin_invoice_by_payment_id(payment.id))

    async def save_admin_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription

    async def save_admin_payment(self, payment: Payment) -> None:
        self.payments[payment.id] = payment

    async def save_admin_invoice(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice

    async def save_subscription_adjustment_audit_record(
        self,
        *,
        audit_id: str,
        subscription_id: str,
        admin_id: str,
        request_id: str,
        adjustment_type: str,
        reason_code: str,
        reason_message: str,
        previous: dict[str, object],
        next_value: dict[str, object],
        notified_customer: bool,
        request_ip: str | None = None,
        result: str = "succeeded",
        idempotency_key_id: str | None = None,
        idempotency_scope: str | None = None,
        idempotency_key_hash: str | None = None,
        idempotency_request_hash: str | None = None,
    ) -> None:
        self.audit_records.append(
            {
                "audit_id": audit_id,
                "subscription_id": subscription_id,
                "admin_id": admin_id,
                "request_id": request_id,
                "adjustment_type": adjustment_type,
                "reason_code": reason_code,
                "reason_message": reason_message,
                "previous": previous,
                "next": next_value,
                "notified_customer": notified_customer,
                "request_ip": request_ip,
                "result": result,
                "idempotency_key_id": idempotency_key_id,
                "idempotency_scope": idempotency_scope,
                "idempotency_key_hash": idempotency_key_hash,
                "idempotency_request_hash": idempotency_request_hash,
            }
        )


class FakeBillingAuthRepository:
    def __init__(self) -> None:
        self.customer_keys: dict[str, str] = {}
        self.active_method_counts: dict[str, int] = {}
        self.auths: dict[str, BillingAuth] = {}
        self.instruments: list[object] = []
        self.methods: list[BillingMethod] = []
        self.default_cleared_for: str | None = None
        self.saved_auths: list[object] = []

    async def get_customer_key_for_user(self, user_id: str) -> str | None:
        return self.customer_keys.get(user_id)

    async def save_customer_key_for_user(
        self,
        user_id: str,
        customer_key: str,
    ) -> None:
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

    async def save_billing_method(self, billing_method: BillingMethod) -> None:
        self.methods.append(billing_method)


class FakePaymentProvider(PaymentProvider):
    def __init__(self) -> None:
        self.confirm_payment_call_count = 0
        self.cancel_payment_call_count = 0
        self.issue_billing_key_call_count = 0
        self.charge_billing_key_call_count = 0
        self.last_billing_charge_customer_key: str | None = None
        self.last_billing_charge_billing_key: str | None = None
        self.last_billing_charge_idempotency_key: str | None = None
        self.last_confirm_payment_idempotency_key: str | None = None
        self.last_cancel_payment_idempotency_key: str | None = None
        self.last_cancel_payment_refund_bank_account: dict[str, object] | None = None
        self.charge_billing_key_error: ProviderError | None = None
        self.charge_billing_key_result: BillingChargeProviderResult | None = None
        self.confirm_payment_error: ProviderError | None = None
        self.confirm_payment_result: PaymentConfirmProviderResult | None = None
        self.issue_billing_key_error: ProviderError | None = None
        self.cancel_payment_error: ProviderError | None = None
        self.cancel_payment_result: PaymentCancelProviderResult | None = None
        self.get_payment_call_count = 0
        self.get_payment_error: ProviderError | None = None
        self.get_payment_result: PaymentLookupProviderResult | None = None
        self.before_get_payment: Callable[[], None] | None = None

    async def confirm_payment(
        self,
        *,
        payment_key: str,
        order_id: str,
        amount: int,
        idempotency_key: str | None = None,
    ) -> PaymentConfirmProviderResult:
        self.confirm_payment_call_count += 1
        self.last_confirm_payment_idempotency_key = idempotency_key
        if self.confirm_payment_error is not None:
            raise self.confirm_payment_error
        if self.confirm_payment_result is not None:
            return self.confirm_payment_result
        return PaymentConfirmProviderResult(
            payment_key=payment_key,
            order_id=order_id,
            amount=amount,
            approved_at=datetime(2026, 6, 10, 0, 1, tzinfo=UTC),
            receipt_url="https://dashboard.tosspayments.com/receipt/payment",
            method="카드",
            method_detail={"maskedCardNumber": "**** **** **** 1234"},
            response_summary={"provider": "tosspayments"},
        )

    async def cancel_payment(
        self,
        *,
        payment_key: str,
        cancel_amount: int,
        cancel_reason: str,
        refund_bank_account: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentCancelProviderResult:
        self.cancel_payment_call_count += 1
        self.last_cancel_payment_idempotency_key = idempotency_key
        self.last_cancel_payment_refund_bank_account = refund_bank_account
        if self.cancel_payment_error is not None:
            raise self.cancel_payment_error
        if self.cancel_payment_result is not None:
            return self.cancel_payment_result
        return PaymentCancelProviderResult(
            cancel_id="cnl_123",
            cancel_amount=cancel_amount,
            canceled_amount=cancel_amount,
            cancelable_amount=0,
            canceled_at=datetime(2026, 6, 10, 0, 2, tzinfo=UTC),
            receipt_url="https://dashboard.tosspayments.com/receipt/cancel",
        )

    async def get_payment(
        self,
        *,
        payment_key: str,
    ) -> PaymentLookupProviderResult:
        self.get_payment_call_count += 1
        if self.before_get_payment is not None:
            self.before_get_payment()
        if self.get_payment_error is not None:
            raise self.get_payment_error
        if self.get_payment_result is not None:
            return self.get_payment_result
        return PaymentLookupProviderResult(
            payment_key=payment_key,
            order_id="order_sync",
            status="DONE",
            total_amount=9900,
            approved_at=datetime(2026, 6, 10, 0, 1, tzinfo=UTC),
            receipt_url="https://dashboard.tosspayments.com/receipt/payment",
            method="카드",
            method_detail={"maskedCardNumber": "**** **** **** 1234"},
            response_summary={"provider": "tosspayments", "status": "DONE"},
            cancelable_amount=9900,
        )

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
    ) -> BillingChargeProviderResult:
        self.charge_billing_key_call_count += 1
        self.last_billing_charge_customer_key = customer_key
        self.last_billing_charge_billing_key = billing_key
        self.last_billing_charge_idempotency_key = idempotency_key
        if self.charge_billing_key_error is not None:
            raise self.charge_billing_key_error
        if self.charge_billing_key_result is not None:
            return self.charge_billing_key_result
        return BillingChargeProviderResult(
            payment_key="paykey_billing_charge",
            order_id=order_id,
            amount=amount,
            approved_at=datetime(2026, 6, 10, 0, 1, tzinfo=UTC),
            receipt_url="https://dashboard.tosspayments.com/receipt/billing",
            method="카드",
            method_detail={"maskedCardNumber": "**** **** **** 1234"},
            response_summary={"provider": "tosspayments"},
        )


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


class FakeCheckoutRepository(CheckoutRepository):
    def __init__(self) -> None:
        self.checkouts: dict[str, Checkout] = {}

    async def save_checkout(self, checkout: Checkout) -> None:
        self.checkouts[checkout.id] = checkout

    async def get_checkout_for_user(
        self,
        checkout_id: str,
        user_id: str,
    ) -> Checkout | None:
        checkout = self.checkouts.get(checkout_id)
        if checkout and checkout.user_id == user_id:
            return checkout
        return None

    async def get_checkout(self, checkout_id: str) -> Checkout | None:
        return self.checkouts.get(checkout_id)

    async def mark_checkout_paid_if_ready(
        self,
        checkout_id: str,
        user_id: str,
        last_payment_id: str,
    ) -> bool:
        checkout = self.checkouts.get(checkout_id)
        if checkout is None or checkout.user_id != user_id:
            return False
        if checkout.status != "ready":
            return False
        checkout.status = "paid"
        checkout.last_payment_id = last_payment_id
        return True


class FakePaymentAttemptRepository(PaymentAttemptRepository):
    def __init__(self, checkouts: FakeCheckoutRepository) -> None:
        self._checkouts = checkouts
        self.payments: dict[str, Payment] = {}

    async def save_payment(self, payment: Payment) -> None:
        self.payments[payment.id] = payment

    async def get_payment(self, payment_id: str) -> Payment | None:
        return self.payments.get(payment_id)

    async def get_payment_for_user(
        self,
        payment_id: str,
        user_id: str,
    ) -> Payment | None:
        payment = self.payments.get(payment_id)
        if payment is None or payment.checkout_id is None:
            return None
        checkout = self._checkouts.checkouts.get(payment.checkout_id)
        if checkout and checkout.user_id == user_id:
            return payment
        return None

    async def count_payments_for_checkout(self, checkout_id: str) -> int:
        return sum(
            1
            for payment in self.payments.values()
            if payment.checkout_id == checkout_id
        )

    async def get_payment_attempt_no(self, checkout_id: str, payment_id: str) -> int:
        attempt_no = 0
        for payment in self.payments.values():
            if payment.checkout_id != checkout_id:
                continue
            attempt_no += 1
            if payment.id == payment_id:
                return attempt_no
        return max(attempt_no, 1)

    async def count_user_payment_quantity_for_sku(
        self,
        user_id: str,
        sku_id: str,
        statuses: set[str],
    ) -> int:
        quantity = 0
        for payment in self.payments.values():
            if payment.status not in statuses or payment.checkout_id is None:
                continue
            checkout = self._checkouts.checkouts.get(payment.checkout_id)
            if checkout is None or checkout.user_id != user_id:
                continue
            for item in checkout.items:
                if item.get("skuId") == sku_id and isinstance(
                    item.get("quantity"),
                    int,
                ):
                    quantity += item["quantity"]
        return quantity


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


class FakePaymentCancelRequestRepository(PaymentCancelRequestRepository):
    def __init__(self) -> None:
        self.payment_cancel_requests: dict[str, PaymentCancelRequest] = {}

    async def find_payment_cancel_request(
        self,
        payment_id: str,
        idempotency_key_hash: str,
    ) -> PaymentCancelRequest | None:
        return next(
            (
                cancel_request
                for cancel_request in self.payment_cancel_requests.values()
                if cancel_request.payment_id == payment_id
                and cancel_request.idempotency_key_hash == idempotency_key_hash
            ),
            None,
        )

    async def save_payment_cancel_request(
        self,
        payment_cancel_request: PaymentCancelRequest,
    ) -> None:
        self.payment_cancel_requests[payment_cancel_request.id] = (
            payment_cancel_request
        )


class FakeOneTimeSkuRepository(OneTimeSkuRepository):
    def __init__(self) -> None:
        self.one_time_skus = {
            "sku_report_pack_100": OneTimeSku(
                id="sku_report_pack_100",
                product_id="product_reports",
                sku_code="REPORT_PACK_100",
                amount=25000,
                stock_policy="unlimited",
                status="active",
            )
        }

    async def get_active_one_time_sku(self, sku_id: str) -> OneTimeSku | None:
        sku = self.one_time_skus.get(sku_id)
        if sku and sku.status == "active":
            return sku
        return None

    async def reserve_one_time_sku_stock(
        self,
        sku: OneTimeSku,
        quantity: int,
    ) -> bool:
        if sku.stock_policy == "unlimited":
            return True
        available_stock = sku.available_stock
        if available_stock is None or available_stock < quantity:
            return False
        sku.reserved_stock = (sku.reserved_stock or 0) + quantity
        return True

    async def release_reserved_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        sku = self.one_time_skus.get(sku_id)
        if sku is None or sku.stock_policy == "unlimited":
            return
        sku.reserved_stock = max((sku.reserved_stock or 0) - quantity, 0)

    async def capture_reserved_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        sku = self.one_time_skus.get(sku_id)
        if sku is None or sku.stock_policy == "unlimited":
            return
        sku.reserved_stock = max((sku.reserved_stock or 0) - quantity, 0)
        sku.sold_stock = (sku.sold_stock or 0) + quantity

    async def restore_sold_one_time_sku_stock(
        self,
        sku_id: str,
        quantity: int,
    ) -> None:
        sku = self.one_time_skus.get(sku_id)
        if sku is None or sku.stock_policy == "unlimited":
            return
        sku.sold_stock = max((sku.sold_stock or 0) - quantity, 0)


class FakeSubscriptionExpirationRepository:
    def __init__(self) -> None:
        self.subscriptions: dict[str, Subscription] = {}

    async def list_expired_cancel_scheduled_subscriptions(
        self,
        now: datetime,
        limit: int,
    ) -> list[Subscription]:
        return [
            subscription
            for subscription in self.subscriptions.values()
            if subscription.status == "cancel_scheduled"
            and _subscription_cancel_expired(subscription, now)
        ][:limit]

    async def expire_cancel_scheduled_subscription(
        self,
        subscription_id: str,
        now: datetime,
    ) -> bool:
        subscription = self.subscriptions[subscription_id]
        if subscription.status != "cancel_scheduled" or not (
            _subscription_cancel_expired(subscription, now)
        ):
            return False
        subscription.status = "canceled"
        subscription.cancel_at_period_end = False
        subscription.canceled_at = now
        subscription.access_until = subscription.current_period_end_at
        subscription.next_billing_at = None
        return True


def _subscription_cancel_expired(
    subscription: Subscription,
    now: datetime,
) -> bool:
    return any(
        value is not None and value <= now
        for value in (subscription.current_period_end_at, subscription.cancel_at)
    )


class FakeSubscriptionExpirationUnitOfWork(SubscriptionExpirationUnitOfWork):
    def __init__(self, factory: FakeSubscriptionExpirationUnitOfWorkFactory) -> None:
        self._factory = factory
        self.subscriptions = factory.subscriptions
        self.operator_audits = factory.operator_audits

    async def __aenter__(self) -> FakeSubscriptionExpirationUnitOfWork:
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


class FakeSubscriptionExpirationUnitOfWorkFactory(
    SubscriptionExpirationUnitOfWorkFactory
):
    def __init__(
        self,
        *,
        subscriptions: FakeSubscriptionExpirationRepository,
        operator_audits: FakeOperatorAuditRepository,
    ) -> None:
        self.subscriptions = subscriptions
        self.operator_audits = operator_audits
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeSubscriptionExpirationUnitOfWork:
        return FakeSubscriptionExpirationUnitOfWork(self)


class FakeSubscriptionAccountRepository:
    def __init__(self) -> None:
        self.records: dict[str, list[SubscriptionAccountRecord]] = {}
        self.billing_methods: dict[str, DefaultBillingMethodSummary] = {}
        self.subscriptions: dict[str, Subscription] = {}

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
        subscription = self.subscriptions.get(subscription_id)
        if subscription is None or subscription.user_id != user_id:
            return None
        return subscription

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
        subscription = self.subscriptions[subscription_id]
        subscription.status = "active"
        subscription.cancel_at_period_end = False
        subscription.cancel_at = None
        subscription.next_billing_at = subscription.current_period_end_at
        subscription.access_until = None
        return subscription

    async def save_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription


class FakeSubscriptionCheckoutRepository:
    def __init__(self) -> None:
        self.active_counts: dict[tuple[str, str], int] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.payments: dict[str, Payment] = {}
        self.invoices: dict[str, Invoice] = {}

    async def count_active_subscriptions_for_user_product(
        self,
        user_id: str,
        product_code: str,
    ) -> int:
        return self.active_counts.get(
            (user_id, product_code),
            sum(
                1
                for subscription in self.subscriptions.values()
                if subscription.user_id == user_id
                and subscription.product_code == product_code
                and subscription.status
                in {"pending", "active", "past_due", "cancel_scheduled"}
            ),
        )

    async def save_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription

    async def get_subscription_for_user(
        self,
        subscription_id: str,
        user_id: str,
    ) -> Subscription | None:
        subscription = self.subscriptions.get(subscription_id)
        if subscription is None or subscription.user_id != user_id:
            return None
        return subscription

    async def get_subscription(
        self,
        subscription_id: str,
    ) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def save_payment(self, payment: Payment) -> None:
        self.payments[payment.id] = payment

    async def save_invoice(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice

    async def get_open_invoice_for_subscription_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Invoice | None:
        return next(
            (
                invoice
                for invoice in self.invoices.values()
                if invoice.subscription_id == subscription_id
                and invoice.billing_cycle_key == billing_cycle_key
                and invoice.status in {"issued", "paid"}
            ),
            None,
        )


class FakeSubscriptionChangeTokenCodec(HmacSubscriptionChangeTokenCodec):
    def __init__(self) -> None:
        super().__init__("test-subscription-change-token-secret")


class FakeBillingMethodRepository:
    def __init__(self) -> None:
        self.records: dict[str, list[BillingMethodRecord]] = {}
        self.method_owners: dict[str, str] = {}
        self.active_subscription_counts: dict[str, int] = {}

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
        changed_at,
    ) -> str | None:
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
        return previous_default_id

    async def deactivate_billing_method_for_user(
        self,
        billing_method_id: str,
        user_id: str,
        deleted_at,
    ) -> None:
        self.records[user_id] = [
            record
            for record in self.records.get(user_id, [])
            if record.billing_method_id != billing_method_id
        ]


class FakeInvoiceRepository:
    def __init__(self) -> None:
        self.invoices: dict[str, Invoice] = {}
        self.records: dict[str, list[InvoiceListRecord]] = {}
        self.details: dict[tuple[str, str], InvoiceDetailRecord] = {}
        self.owners: dict[str, str] = {}

    async def list_invoices_for_user(
        self,
        user_id: str,
        limit: int,
        status=None,
        payment_status=None,
        subscription_id: str | None = None,
        from_date=None,
        to_date=None,
        cursor: str | None = None,
    ) -> list[InvoiceListRecord]:
        _ = cursor
        records = self.records.get(user_id, [])
        if status is not None:
            records = [record for record in records if record.status == status]
        if payment_status is not None:
            records = [
                record
                for record in records
                if record.payment_status == payment_status
            ]
        if subscription_id is not None:
            records = [
                record
                for record in records
                if record.subscription_id == subscription_id
            ]
        if from_date is not None:
            records = [
                record for record in records if record.billing_date >= from_date
            ]
        if to_date is not None:
            records = [
                record for record in records if record.billing_date <= to_date
            ]
        records = sorted(
            records,
            key=lambda record: (record.billing_date, record.invoice_id),
            reverse=True,
        )
        if cursor is not None:
            payload = decode_cursor(cursor)
            billing_date = date.fromisoformat(str(payload["billingDate"]))
            invoice_id = str(payload["invoiceId"])
            records = [
                record
                for record in records
                if record.billing_date < billing_date
                or (
                    record.billing_date == billing_date
                    and record.invoice_id < invoice_id
                )
            ]
        return records[:limit]

    async def get_invoice_detail_for_user(
        self,
        invoice_id: str,
        user_id: str,
    ) -> InvoiceDetailRecord | None:
        return self.details.get((user_id, invoice_id))

    async def get_invoice_owner(self, invoice_id: str) -> str | None:
        for user_id, records in self.records.items():
            if any(record.invoice_id == invoice_id for record in records):
                return user_id
        for user_id, invoice_id_key in self.details:
            if invoice_id_key == invoice_id:
                return user_id
        return self.owners.get(invoice_id)

    async def save_invoice(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice


class FakeBillingRetryRepository:
    def __init__(self) -> None:
        self.invoices: dict[str, Invoice] = {}
        self.payments: dict[str, Payment] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.subscription_plans: dict[str, SubscriptionPlan] = {}
        self.billing_methods: dict[str, BillingMethod] = {}
        self.instruments: dict[str, PaymentInstrument] = {}

    async def list_due_active_subscriptions(
        self,
        billing_cutoff_at,
        limit: int,
    ) -> list[Subscription]:
        return [
            copy(subscription)
            for subscription in sorted(
                self.subscriptions.values(),
                key=lambda item: item.next_billing_at or billing_cutoff_at,
            )
            if subscription.status == "active"
            and subscription.next_billing_at is not None
            and subscription.next_billing_at <= billing_cutoff_at
        ][:limit]

    async def list_reminder_subscriptions(
        self,
        reminder_start_at,
        reminder_end_at,
        limit: int,
    ) -> list[Subscription]:
        return [
            subscription
            for subscription in sorted(
                self.subscriptions.values(),
                key=lambda item: item.next_billing_at or reminder_start_at,
            )
            if subscription.status == "active"
            and subscription.next_billing_at is not None
            and reminder_start_at <= subscription.next_billing_at <= reminder_end_at
        ][:limit]

    async def count_excluded_billing_subscriptions(self) -> int:
        return sum(
            1
            for subscription in self.subscriptions.values()
            if subscription.status == "cancel_scheduled"
            or (
                subscription.status == "active"
                and subscription.next_billing_at is None
            )
        )

    async def get_subscription_plan(
        self,
        plan_id: str,
    ) -> SubscriptionPlan | None:
        return self.subscription_plans.get(plan_id)

    async def get_invoice_by_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Invoice | None:
        return next(
            (
                invoice
                for invoice in self.invoices.values()
                if invoice.subscription_id == subscription_id
                and invoice.billing_cycle_key == billing_cycle_key
                and invoice.status in {"issued", "paid"}
            ),
            None,
        )

    async def get_invoice(self, invoice_id: str) -> Invoice | None:
        return self.invoices.get(invoice_id)

    async def get_payment(self, payment_id: str) -> Payment | None:
        return self.payments.get(payment_id)

    async def get_latest_failed_payment_for_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> Payment | None:
        failed_payments = [
            payment
            for payment in self.payments.values()
            if payment.subscription_id == subscription_id
            and payment.billing_cycle_key == billing_cycle_key
            and payment.status == "failed"
        ]
        if not failed_payments:
            return None
        return max(
            failed_payments,
            key=lambda payment: (payment.created_at, payment.id),
        )

    async def count_failed_payments_for_billing_cycle(
        self,
        subscription_id: str,
        billing_cycle_key: str,
    ) -> int:
        return sum(
            1
            for payment in self.payments.values()
            if payment.subscription_id == subscription_id
            and payment.billing_cycle_key == billing_cycle_key
            and payment.status == "failed"
        )

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def get_default_billing_method(self, user_id: str) -> BillingMethod | None:
        return next(
            (
                method
                for method in self.billing_methods.values()
                if method.user_id == user_id
                and method.is_default
                and method.status == "active"
            ),
            None,
        )

    async def get_payment_instrument(
        self,
        instrument_id: str,
    ) -> PaymentInstrument | None:
        return self.instruments.get(instrument_id)

    async def save_payment(self, payment: Payment) -> None:
        self.payments[payment.id] = payment

    async def save_invoice(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice

    async def save_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription

    async def save_subscription_billing_result(
        self,
        *,
        payment: Payment,
        invoice: Invoice,
        subscription: Subscription,
        expected_next_billing_at: datetime,
    ) -> bool:
        current = self.subscriptions.get(subscription.id)
        if (
            current is None
            or current.status != "active"
            or current.next_billing_at != expected_next_billing_at
        ):
            return False
        self.payments[payment.id] = payment
        self.invoices[invoice.id] = invoice
        self.subscriptions[subscription.id] = subscription
        return True


class FakeWebhookRepository:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], WebhookEvent] = {}
        self.payments: dict[str, Payment] = {}
        self.checkouts: dict[str, Checkout] = {}
        self.one_time_skus: dict[str, OneTimeSku] = {}
        self.invoices: dict[str, Invoice] = {}
        self.subscriptions: dict[str, Subscription] = {}

    async def get_webhook_event(
        self,
        provider: str,
        event_id: str,
    ) -> WebhookEvent | None:
        return self.events.get((provider, event_id))

    async def get_processed_webhook_event_by_payment_status(
        self,
        *,
        provider: str,
        payment_key: str,
        provider_status: str,
        exclude_event_id: str,
    ) -> WebhookEvent | None:
        return next(
            (
                event
                for event in self.events.values()
                if event.provider == provider
                and event.event_id != exclude_event_id
                and event.payment_key == payment_key
                and event.payload.get("status") == provider_status
                and event.status in {"processed", "ignored"}
            ),
            None,
        )

    async def save_webhook_event(self, event: WebhookEvent) -> None:
        self.events[(event.provider, event.event_id)] = event

    async def get_payment_by_order_or_key(
        self,
        *,
        order_id: str | None,
        payment_key: str | None,
    ) -> Payment | None:
        return next(
            (
                payment
                for payment in self.payments.values()
                if payment.order_id == order_id or payment.payment_key == payment_key
            ),
            None,
        )

    async def save_payment(self, payment: Payment) -> None:
        self.payments[payment.id] = payment

    async def get_checkout(self, checkout_id: str) -> Checkout | None:
        return self.checkouts.get(checkout_id)

    async def mark_checkout_paid_if_ready(
        self,
        checkout_id: str,
        user_id: str,
        last_payment_id: str,
    ) -> bool:
        checkout = self.checkouts.get(checkout_id)
        if (
            checkout is None
            or checkout.user_id != user_id
            or checkout.status != "ready"
        ):
            return False
        checkout.status = "paid"
        checkout.last_payment_id = last_payment_id
        return True

    async def capture_checkout_reserved_stock(self, checkout: Checkout) -> None:
        for item in checkout.items:
            sku_id = item.get("skuId")
            quantity = item.get("quantity")
            if not isinstance(sku_id, str) or not isinstance(quantity, int):
                continue
            sku = self.one_time_skus.get(sku_id)
            if sku is None or sku.stock_policy != "limited":
                continue
            sku.reserved_stock = max((sku.reserved_stock or 0) - quantity, 0)
            sku.sold_stock = (sku.sold_stock or 0) + quantity

    async def get_invoice_by_payment_id(self, payment_id: str) -> Invoice | None:
        return next(
            (
                invoice
                for invoice in self.invoices.values()
                if invoice.payment_id == payment_id
            ),
            None,
        )

    async def save_invoice(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        return self.subscriptions.get(subscription_id)

    async def save_subscription(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription


class FakeWebhookUnitOfWork(WebhookUnitOfWork):
    def __init__(self, factory: FakeWebhookUnitOfWorkFactory) -> None:
        self._factory = factory
        self.webhooks = factory.webhooks

    async def __aenter__(self) -> FakeWebhookUnitOfWork:
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


class FakeWebhookUnitOfWorkFactory(WebhookUnitOfWorkFactory):
    def __init__(self, webhooks: FakeWebhookRepository) -> None:
        self.webhooks = webhooks
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeWebhookUnitOfWork:
        return FakeWebhookUnitOfWork(self)


class FakeOperatorAuditRepository(OperatorAuditRepository):
    def __init__(self) -> None:
        self.operator_audits: dict[str, OperatorAudit] = {}

    async def list_operator_audits(
        self,
        query: OperatorAuditQuery,
    ) -> list[OperatorAudit]:
        audits = list(self.operator_audits.values())
        if query.operator_id is not None:
            audits = [
                audit for audit in audits if audit.operator_id == query.operator_id
            ]
        if query.action is not None:
            audits = [audit for audit in audits if audit.action == query.action]
        if query.target_type is not None:
            audits = [
                audit for audit in audits if audit.target_type == query.target_type
            ]
        if query.target_id is not None:
            audits = [audit for audit in audits if audit.target_id == query.target_id]
        if query.result is not None:
            audits = [audit for audit in audits if audit.result in query.result]
        if query.from_at is not None:
            audits = [
                audit for audit in audits if audit.created_at >= query.from_at
            ]
        if query.to_at is not None:
            audits = [audit for audit in audits if audit.created_at <= query.to_at]
        audits = sorted(
            audits,
            key=lambda audit: (audit.created_at, audit.id),
            reverse=True,
        )
        if query.cursor is not None:
            payload = decode_cursor(query.cursor)
            created_at = datetime.fromisoformat(
                str(payload["createdAt"]).replace("Z", "+00:00")
            )
            audit_id = str(payload["auditId"])
            audits = [
                audit
                for audit in audits
                if (audit.created_at, audit.id) < (created_at, audit_id)
            ]
        return audits[: query.limit]

    async def get_operator_audit(self, audit_id: str) -> OperatorAudit | None:
        return self.operator_audits.get(audit_id)

    async def save_operator_audit(self, audit: OperatorAudit) -> None:
        self.operator_audits[audit.id] = audit


class FakeSchedulerRunRepository(SchedulerRunLogRepository):
    def __init__(self) -> None:
        self.runs: dict[str, SchedulerRunLog] = {}

    async def list_scheduler_runs(
        self,
        query: SchedulerRunQuery,
    ) -> list[SchedulerRunLog]:
        runs = list(self.runs.values())
        if query.job_type is not None:
            runs = [run for run in runs if run.job_type in query.job_type]
        if query.status is not None:
            runs = [run for run in runs if run.status in query.status]
        if query.trigger_source is not None:
            runs = [
                run for run in runs if run.trigger_source in query.trigger_source
            ]
        if query.worker_id is not None:
            runs = [run for run in runs if run.worker_id == query.worker_id]
        if query.from_at is not None:
            runs = [run for run in runs if run.started_at >= query.from_at]
        if query.to_at is not None:
            runs = [run for run in runs if run.started_at <= query.to_at]
        runs = sorted(runs, key=lambda run: (run.started_at, run.id), reverse=True)
        if query.cursor is not None:
            payload = decode_cursor(query.cursor)
            started_at = datetime.fromisoformat(
                str(payload["startedAt"]).replace("Z", "+00:00")
            )
            run_id = str(payload["runId"])
            runs = [
                run
                for run in runs
                if (run.started_at, run.id) < (started_at, run_id)
            ]
        return runs[: query.limit]

    async def get_scheduler_run(self, run_id: str) -> SchedulerRunLog | None:
        return self.runs.get(run_id)

    async def save_scheduler_run(self, run: SchedulerRunLog) -> None:
        self.runs[run.id] = run


class FakeOperationLockRepository(OperationLockRepository):
    def __init__(self) -> None:
        self.operation_locks: dict[str, OperationLock] = {}
        self.fencing_tokens: dict[str, int] = {}
        self.acquire_calls: list[str] = []
        self.release_calls: list[str] = []

    async def acquire_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        fencing_counter_key: str,
        locked_until_at: datetime,
        acquired_at: datetime,
        metadata: dict[str, object] | None = None,
    ) -> OperationLock | None:
        self.acquire_calls.append(lock_key)
        existing = self.operation_locks.get(lock_key)
        if (
            existing is not None
            and existing.status == "active"
            and existing.locked_until_at > acquired_at
        ):
            return None
        fencing_token = self.fencing_tokens.get(fencing_counter_key, 0) + 1
        self.fencing_tokens[fencing_counter_key] = fencing_token
        operation_lock = OperationLock(
            id=OperationLock.generate_id(),
            lock_key=lock_key,
            owner_token=owner_token,
            fencing_token=fencing_token,
            fencing_counter_key=fencing_counter_key,
            status="active",
            locked_until_at=locked_until_at,
            acquired_at=acquired_at,
            metadata=metadata,
        )
        self.operation_locks[lock_key] = operation_lock
        return operation_lock

    async def release_operation_lock(
        self,
        *,
        lock_key: str,
        owner_token: str,
        released_at: datetime,
    ) -> None:
        self.release_calls.append(lock_key)
        operation_lock = self.operation_locks.get(lock_key)
        if (
            operation_lock is not None
            and operation_lock.owner_token == owner_token
            and operation_lock.status == "active"
        ):
            operation_lock.status = "released"
            operation_lock.released_at = released_at


@dataclass(frozen=True, slots=True)
class FakePaymentStores:
    idempotency_keys: FakeIdempotencyKeyRepository
    checkouts: FakeCheckoutRepository
    invoices: FakeInvoiceRepository
    payments: FakePaymentAttemptRepository
    one_time_skus: FakeOneTimeSkuRepository
    payment_customers: FakePaymentCustomerRepository
    payment_cancel_requests: FakePaymentCancelRequestRepository
    operator_audits: FakeOperatorAuditRepository


class FakeOneTimePaymentUnitOfWork(OneTimePaymentUnitOfWork):
    def __init__(self, stores: FakePaymentStores) -> None:
        self.idempotency_keys: IdempotencyKeyRepository = stores.idempotency_keys
        self.checkouts: CheckoutRepository = stores.checkouts
        self.invoices: InvoiceWriteRepository = stores.invoices
        self.payments: PaymentAttemptRepository = stores.payments
        self.one_time_skus: OneTimeSkuRepository = stores.one_time_skus
        self.payment_customers: PaymentCustomerRepository = stores.payment_customers
        self.payment_cancel_requests: PaymentCancelRequestRepository = (
            stores.payment_cancel_requests
        )
        self.operator_audits: OperatorAuditRepository = stores.operator_audits

    async def __aenter__(self) -> FakeOneTimePaymentUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeOneTimePaymentUnitOfWorkFactory(OneTimePaymentUnitOfWorkFactory):
    def __init__(self, stores: FakePaymentStores) -> None:
        self._stores = stores

    def __call__(self) -> FakeOneTimePaymentUnitOfWork:
        return FakeOneTimePaymentUnitOfWork(self._stores)


class FakeSubscriptionCancelUnitOfWork(SubscriptionCancelUnitOfWork):
    def __init__(self, factory: FakeSubscriptionCancelUnitOfWorkFactory) -> None:
        self._factory = factory
        self.subscriptions = factory.subscriptions
        self.idempotency_keys = factory.idempotency_keys
        self.operator_audits = factory.operator_audits

    async def __aenter__(self) -> FakeSubscriptionCancelUnitOfWork:
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


class FakeSubscriptionCancelUnitOfWorkFactory(
    SubscriptionCancelUnitOfWorkFactory
):
    def __init__(
        self,
        *,
        subscriptions: SubscriptionAccountRepository,
        idempotency_keys: IdempotencyKeyRepository,
        operator_audits: OperatorAuditRepository,
    ) -> None:
        self.subscriptions = subscriptions
        self.idempotency_keys = idempotency_keys
        self.operator_audits = operator_audits
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeSubscriptionCancelUnitOfWork:
        return FakeSubscriptionCancelUnitOfWork(self)


class FakeSubscriptionResumeUnitOfWork(SubscriptionResumeUnitOfWork):
    def __init__(self, factory: FakeSubscriptionResumeUnitOfWorkFactory) -> None:
        self._factory = factory
        self.subscriptions = factory.subscriptions
        self.idempotency_keys = factory.idempotency_keys
        self.operator_audits = factory.operator_audits

    async def __aenter__(self) -> FakeSubscriptionResumeUnitOfWork:
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


class FakeSubscriptionResumeUnitOfWorkFactory(
    SubscriptionResumeUnitOfWorkFactory
):
    def __init__(
        self,
        *,
        subscriptions: SubscriptionAccountRepository,
        idempotency_keys: IdempotencyKeyRepository,
        operator_audits: OperatorAuditRepository,
    ) -> None:
        self.subscriptions = subscriptions
        self.idempotency_keys = idempotency_keys
        self.operator_audits = operator_audits
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeSubscriptionResumeUnitOfWork:
        return FakeSubscriptionResumeUnitOfWork(self)


class FakeSubscriptionChangeUnitOfWork(SubscriptionChangeUnitOfWork):
    def __init__(self, factory: FakeSubscriptionChangeUnitOfWorkFactory) -> None:
        self._factory = factory
        self.billing = factory.billing_repository
        self.subscriptions = factory.subscriptions
        self.idempotency_keys = factory.idempotency_keys
        self.operator_audits = factory.operator_audits

    async def __aenter__(self) -> FakeSubscriptionChangeUnitOfWork:
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


class FakeSubscriptionChangeUnitOfWorkFactory(
    SubscriptionChangeUnitOfWorkFactory
):
    def __init__(
        self,
        *,
        billing_repository: BillingRetryRepository,
        subscriptions: SubscriptionAccountRepository,
        idempotency_keys: IdempotencyKeyRepository,
        operator_audits: OperatorAuditRepository,
    ) -> None:
        self.billing_repository = billing_repository
        self.subscriptions = subscriptions
        self.idempotency_keys = idempotency_keys
        self.operator_audits = operator_audits
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeSubscriptionChangeUnitOfWork:
        return FakeSubscriptionChangeUnitOfWork(self)


class FakeSubscriptionConfirmUnitOfWork(SubscriptionConfirmUnitOfWork):
    def __init__(
        self,
        factory: FakeSubscriptionConfirmUnitOfWorkFactory,
    ) -> None:
        self._factory = factory
        self.billing_auths = factory.billing_auths
        self.subscriptions = factory.subscriptions
        self.idempotency_keys = factory.idempotency_keys

    async def __aenter__(self) -> FakeSubscriptionConfirmUnitOfWork:
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


class FakeSubscriptionConfirmUnitOfWorkFactory(
    SubscriptionConfirmUnitOfWorkFactory
):
    def __init__(
        self,
        *,
        billing_auths: FakeBillingAuthRepository,
        subscriptions: FakeSubscriptionCheckoutRepository,
        idempotency_keys: FakeIdempotencyKeyRepository,
    ) -> None:
        self.billing_auths = billing_auths
        self.subscriptions = subscriptions
        self.idempotency_keys = idempotency_keys
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeSubscriptionConfirmUnitOfWork:
        return FakeSubscriptionConfirmUnitOfWork(self)


class FakeBillingAuthIssueUnitOfWork(BillingAuthIssueUnitOfWork):
    def __init__(
        self,
        factory: FakeBillingAuthIssueUnitOfWorkFactory,
    ) -> None:
        self._factory = factory
        self.billing_auths = factory.billing_auths
        self.idempotency_keys = factory.idempotency_keys

    async def __aenter__(self) -> FakeBillingAuthIssueUnitOfWork:
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


class FakeBillingAuthIssueUnitOfWorkFactory(BillingAuthIssueUnitOfWorkFactory):
    def __init__(
        self,
        *,
        billing_auths: FakeBillingAuthRepository,
        idempotency_keys: FakeIdempotencyKeyRepository,
    ) -> None:
        self.billing_auths = billing_auths
        self.idempotency_keys = idempotency_keys
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeBillingAuthIssueUnitOfWork:
        return FakeBillingAuthIssueUnitOfWork(self)


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


class FakeBillingMethodDeleteUnitOfWork(BillingMethodDeleteUnitOfWork):
    def __init__(self, factory: FakeBillingMethodDeleteUnitOfWorkFactory) -> None:
        self._factory = factory
        self.billing_methods = factory.billing_methods
        self.idempotency_keys = factory.idempotency_keys
        self.operator_audits = factory.operator_audits

    async def __aenter__(self) -> FakeBillingMethodDeleteUnitOfWork:
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


class FakeBillingMethodDeleteUnitOfWorkFactory(
    BillingMethodDeleteUnitOfWorkFactory
):
    def __init__(
        self,
        *,
        billing_methods: FakeBillingMethodRepository,
        idempotency_keys: FakeIdempotencyKeyRepository,
        operator_audits: FakeOperatorAuditRepository,
    ) -> None:
        self.billing_methods = billing_methods
        self.idempotency_keys = idempotency_keys
        self.operator_audits = operator_audits
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeBillingMethodDeleteUnitOfWork:
        return FakeBillingMethodDeleteUnitOfWork(self)


class FakeSubscriptionBillingUnitOfWork(SubscriptionBillingUnitOfWork):
    def __init__(self, factory: FakeSubscriptionBillingUnitOfWorkFactory) -> None:
        self._factory = factory
        self.billing = factory.billing
        self.idempotency_keys = factory.idempotency_keys

    async def __aenter__(self) -> FakeSubscriptionBillingUnitOfWork:
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


class FakeSubscriptionBillingUnitOfWorkFactory(
    SubscriptionBillingUnitOfWorkFactory
):
    def __init__(
        self,
        billing: BillingRetryRepository,
        idempotency_keys: IdempotencyKeyRepository | None = None,
    ) -> None:
        self.billing = billing
        self.idempotency_keys = idempotency_keys or FakeIdempotencyKeyRepository()
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeSubscriptionBillingUnitOfWork:
        return FakeSubscriptionBillingUnitOfWork(self)


class FakeAdminSubscriptionAdjustUnitOfWork(AdminSubscriptionAdjustUnitOfWork):
    def __init__(
        self,
        factory: FakeAdminSubscriptionAdjustUnitOfWorkFactory,
    ) -> None:
        self._factory = factory
        self.admin_operations = factory.admin_operations
        self.idempotency_keys = factory.idempotency_keys

    async def __aenter__(self) -> FakeAdminSubscriptionAdjustUnitOfWork:
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


class FakeAdminSubscriptionAdjustUnitOfWorkFactory(
    AdminSubscriptionAdjustUnitOfWorkFactory
):
    def __init__(
        self,
        *,
        admin_operations: FakeAdminOperationsRepository,
        idempotency_keys: FakeIdempotencyKeyRepository,
    ) -> None:
        self.admin_operations = admin_operations
        self.idempotency_keys = idempotency_keys
        self.enter_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def __call__(self) -> FakeAdminSubscriptionAdjustUnitOfWork:
        return FakeAdminSubscriptionAdjustUnitOfWork(self)


@dataclass(frozen=True, slots=True)
class TestDependencies:
    admin_catalog: FakeAdminCatalogRepository
    admin_auth: FakeAdminAuthRepository
    admin_auth_uow_factory: FakeAdminAuthUnitOfWorkFactory
    admin_auth_email_sender: FakeAdminAuthEmailSender
    admin_auth_rate_limiter: FakeAdminAuthRateLimiter
    admin_operations: FakeAdminOperationsRepository
    scheduler_runs: FakeSchedulerRunRepository
    admin_subscription_adjust_uow_factory: (
        FakeAdminSubscriptionAdjustUnitOfWorkFactory
    )
    billing_auths: FakeBillingAuthRepository
    billing_auth_issue_uow_factory: FakeBillingAuthIssueUnitOfWorkFactory
    catalog_repository: FakeCatalogRepository
    billing_methods: FakeBillingMethodRepository
    billing_method_default_uow_factory: FakeBillingMethodDefaultUnitOfWorkFactory
    billing_method_delete_uow_factory: FakeBillingMethodDeleteUnitOfWorkFactory
    billing_retries: FakeBillingRetryRepository
    invoices: FakeInvoiceRepository
    operation_locks: FakeOperationLockRepository
    payment_stores: FakePaymentStores
    one_time_payment_uow_factory: FakeOneTimePaymentUnitOfWorkFactory
    payment_attempts: FakePaymentAttemptRepository
    payment_provider: FakePaymentProvider
    subscription_accounts: FakeSubscriptionAccountRepository
    subscription_billing_uow_factory: FakeSubscriptionBillingUnitOfWorkFactory
    subscription_checkouts: FakeSubscriptionCheckoutRepository
    subscription_cancel_uow_factory: FakeSubscriptionCancelUnitOfWorkFactory
    subscription_change_uow_factory: FakeSubscriptionChangeUnitOfWorkFactory
    subscription_confirm_uow_factory: FakeSubscriptionConfirmUnitOfWorkFactory
    subscription_change_tokens: FakeSubscriptionChangeTokenCodec
    subscription_expirations: FakeSubscriptionExpirationRepository
    subscription_expiration_uow_factory: FakeSubscriptionExpirationUnitOfWorkFactory
    subscription_resume_uow_factory: FakeSubscriptionResumeUnitOfWorkFactory
    webhooks: FakeWebhookRepository
    webhook_uow_factory: FakeWebhookUnitOfWorkFactory
    billing_key_cipher: BillingKeyCipher
    clock: FixedClock

    def to_http_dependencies(self) -> HttpDependencies:
        return HttpDependencies(
            admin_catalog=self.admin_catalog,
            admin_auth=self.admin_auth,
            admin_auth_uow_factory=self.admin_auth_uow_factory,
            admin_auth_email_sender=self.admin_auth_email_sender,
            admin_auth_rate_limiter=self.admin_auth_rate_limiter,
            admin_operations=self.admin_operations,
            operator_audits=self.payment_stores.operator_audits,
            scheduler_runs=self.scheduler_runs,
            admin_subscription_adjust_uow_factory=(
                self.admin_subscription_adjust_uow_factory
            ),
            billing_auths=self.billing_auths,
            billing_auth_issue_uow_factory=self.billing_auth_issue_uow_factory,
            catalog_repository=self.catalog_repository,
            billing_methods=self.billing_methods,
            billing_method_default_uow_factory=(
                self.billing_method_default_uow_factory
            ),
            billing_method_delete_uow_factory=(
                self.billing_method_delete_uow_factory
            ),
            billing_retries=self.billing_retries,
            invoices=self.invoices,
            idempotency_keys=self.payment_stores.idempotency_keys,
            operation_locks=self.operation_locks,
            one_time_payment_uow_factory=self.one_time_payment_uow_factory,
            payment_attempts=self.payment_attempts,
            payment_customers=self.payment_stores.payment_customers,
            payment_provider=self.payment_provider,
            subscription_accounts=self.subscription_accounts,
            subscription_billing_uow_factory=self.subscription_billing_uow_factory,
            subscription_checkouts=self.subscription_checkouts,
            subscription_cancel_uow_factory=self.subscription_cancel_uow_factory,
            subscription_change_uow_factory=self.subscription_change_uow_factory,
            subscription_confirm_uow_factory=self.subscription_confirm_uow_factory,
            subscription_change_tokens=self.subscription_change_tokens,
            subscription_expirations=self.subscription_expirations,
            subscription_expiration_uow_factory=(
                self.subscription_expiration_uow_factory
            ),
            subscription_resume_uow_factory=self.subscription_resume_uow_factory,
            notification_enqueue=fake_notification_enqueue_dependencies(),
            webhooks=self.webhooks,
            webhook_uow_factory=self.webhook_uow_factory,
            billing_key_cipher=self.billing_key_cipher,
            clock=self.clock,
            internal_service_token="secret",
            toss_client_key="test_ck_local",
            toss_webhook_secret="webhook-secret",
        )


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer secret",
        "X-Request-Id": "req_test",
        "X-Request-User-Id": "user_1",
    }


@pytest.fixture
def admin_headers(test_dependencies: TestDependencies) -> dict[str, str]:
    now = test_dependencies.clock.utc_now()
    admin = AdminAccount(
        id="admin_1",
        email="admin@example.com",
        email_lower="admin@example.com",
        password_hash="unused",
        display_name="Admin",
        status="active",
        roles=["admin"],
        permissions=[
            "payment_read",
            "payment_cancel",
            "subscription_read",
            "subscription_adjust",
            "scheduler_read",
            "scheduler_run",
            "audit_read",
            "product_manage",
        ],
        permission_version=1,
        failed_login_count=0,
        created_at=now,
        updated_at=now,
    )
    test_dependencies.admin_auth.admin_accounts[admin.id] = admin
    return {
        "Authorization": (
            "Bearer "
            + _sign_access_token(admin, test_dependencies.clock.utc_now(), "secret")
        ),
        "X-Request-Id": "req_admin",
    }


@pytest.fixture
def test_dependencies() -> TestDependencies:
    checkouts = FakeCheckoutRepository()
    invoices = FakeInvoiceRepository()
    payment_attempts = FakePaymentAttemptRepository(checkouts)
    billing_auths = FakeBillingAuthRepository()
    subscription_checkouts = FakeSubscriptionCheckoutRepository()
    subscription_accounts = FakeSubscriptionAccountRepository()
    billing_methods = FakeBillingMethodRepository()
    subscription_expirations = FakeSubscriptionExpirationRepository()
    billing_retries = FakeBillingRetryRepository()
    admin_auth = FakeAdminAuthRepository()
    admin_operations = FakeAdminOperationsRepository()
    scheduler_runs = FakeSchedulerRunRepository()
    payment_stores = FakePaymentStores(
        idempotency_keys=FakeIdempotencyKeyRepository(),
        checkouts=checkouts,
        invoices=invoices,
        payments=payment_attempts,
        one_time_skus=FakeOneTimeSkuRepository(),
        payment_customers=FakePaymentCustomerRepository(),
        payment_cancel_requests=FakePaymentCancelRequestRepository(),
        operator_audits=FakeOperatorAuditRepository(),
    )
    webhooks = FakeWebhookRepository()
    return TestDependencies(
        admin_catalog=FakeAdminCatalogRepository(),
        admin_auth=admin_auth,
        admin_auth_uow_factory=FakeAdminAuthUnitOfWorkFactory(admin_auth),
        admin_auth_email_sender=FakeAdminAuthEmailSender(),
        admin_auth_rate_limiter=FakeAdminAuthRateLimiter(),
        admin_operations=admin_operations,
        scheduler_runs=scheduler_runs,
        admin_subscription_adjust_uow_factory=(
            FakeAdminSubscriptionAdjustUnitOfWorkFactory(
                admin_operations=admin_operations,
                idempotency_keys=payment_stores.idempotency_keys,
            )
        ),
        billing_auths=billing_auths,
        billing_auth_issue_uow_factory=FakeBillingAuthIssueUnitOfWorkFactory(
            billing_auths=billing_auths,
            idempotency_keys=payment_stores.idempotency_keys,
        ),
        catalog_repository=FakeCatalogRepository(),
        billing_methods=billing_methods,
        billing_method_default_uow_factory=(
            FakeBillingMethodDefaultUnitOfWorkFactory(billing_methods)
        ),
        billing_method_delete_uow_factory=(
            FakeBillingMethodDeleteUnitOfWorkFactory(
                billing_methods=billing_methods,
                idempotency_keys=payment_stores.idempotency_keys,
                operator_audits=payment_stores.operator_audits,
            )
        ),
        billing_retries=billing_retries,
        invoices=invoices,
        operation_locks=FakeOperationLockRepository(),
        payment_stores=payment_stores,
        one_time_payment_uow_factory=FakeOneTimePaymentUnitOfWorkFactory(
            payment_stores
        ),
        payment_attempts=payment_attempts,
        payment_provider=FakePaymentProvider(),
        subscription_accounts=subscription_accounts,
        subscription_billing_uow_factory=FakeSubscriptionBillingUnitOfWorkFactory(
            billing_retries,
            idempotency_keys=payment_stores.idempotency_keys,
        ),
        subscription_checkouts=subscription_checkouts,
        subscription_cancel_uow_factory=FakeSubscriptionCancelUnitOfWorkFactory(
            subscriptions=subscription_accounts,
            idempotency_keys=payment_stores.idempotency_keys,
            operator_audits=payment_stores.operator_audits,
        ),
        subscription_change_uow_factory=FakeSubscriptionChangeUnitOfWorkFactory(
            billing_repository=billing_retries,
            subscriptions=subscription_accounts,
            idempotency_keys=payment_stores.idempotency_keys,
            operator_audits=payment_stores.operator_audits,
        ),
        subscription_confirm_uow_factory=FakeSubscriptionConfirmUnitOfWorkFactory(
            billing_auths=billing_auths,
            subscriptions=subscription_checkouts,
            idempotency_keys=payment_stores.idempotency_keys,
        ),
        subscription_change_tokens=FakeSubscriptionChangeTokenCodec(),
        subscription_expirations=subscription_expirations,
        subscription_expiration_uow_factory=(
            FakeSubscriptionExpirationUnitOfWorkFactory(
                subscriptions=subscription_expirations,
                operator_audits=payment_stores.operator_audits,
            )
        ),
        subscription_resume_uow_factory=FakeSubscriptionResumeUnitOfWorkFactory(
            subscriptions=subscription_accounts,
            idempotency_keys=payment_stores.idempotency_keys,
            operator_audits=payment_stores.operator_audits,
        ),
        webhooks=webhooks,
        webhook_uow_factory=FakeWebhookUnitOfWorkFactory(webhooks),
        billing_key_cipher=FernetBillingKeyCipher("test-billing-key-secret"),
        clock=FixedClock(),
    )


@pytest.fixture
def client(test_dependencies: TestDependencies) -> Iterator[TestClient]:
    app = create_app(test_dependencies.to_http_dependencies())
    with TestClient(app) as test_client:
        yield test_client
