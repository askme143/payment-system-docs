from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from payments.domain.entities.notification import NotificationTemplate


@dataclass(frozen=True, slots=True)
class NotificationTemplateSpec:
    event_type: str
    required_template_args: tuple[str, ...]
    optional_template_args: tuple[str, ...]
    encrypted_args: tuple[str, ...]


NOTIFICATION_TEMPLATE_SPECS: tuple[NotificationTemplateSpec, ...] = (
    NotificationTemplateSpec(
        event_type="admin_auth.login_link",
        required_template_args=("loginLink", "expiresMinutes"),
        optional_template_args=(
            "recipientName",
            "requestIp",
            "userAgent",
            "supportUrl",
        ),
        encrypted_args=("loginLink",),
    ),
    NotificationTemplateSpec(
        event_type="admin_auth.password_reset",
        required_template_args=("resetLink", "expiresMinutes"),
        optional_template_args=("recipientName", "requestIp", "supportUrl"),
        encrypted_args=("resetLink",),
    ),
    NotificationTemplateSpec(
        event_type="subscription_billing_reminder",
        required_template_args=(
            "subscriptionId",
            "planName",
            "amount",
            "currency",
            "billingDate",
            "subscriptionManageUrl",
        ),
        optional_template_args=(
            "recipientName",
            "productName",
            "billingMethodSummary",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="subscription_payment_paid",
        required_template_args=(
            "subscriptionId",
            "invoiceId",
            "amount",
            "currency",
            "billingDate",
            "receiptUrl",
        ),
        optional_template_args=(
            "recipientName",
            "planName",
            "productName",
            "paidAt",
            "paymentMethodSummary",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="subscription_payment_failed",
        required_template_args=(
            "subscriptionId",
            "invoiceId",
            "amount",
            "currency",
            "failureSummary",
            "retryScheduledAt",
            "billingMethodUpdateUrl",
        ),
        optional_template_args=(
            "recipientName",
            "planName",
            "productName",
            "providerCode",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="subscription_canceled_payment_failed",
        required_template_args=(
            "subscriptionId",
            "invoiceId",
            "canceledAt",
            "failureSummary",
            "cancelReason",
            "subscriptionManageUrl",
            "resubscribeUrl",
        ),
        optional_template_args=(
            "recipientName",
            "amount",
            "currency",
            "providerCode",
            "planName",
            "productName",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="subscription_canceled_after_period",
        required_template_args=(
            "subscriptionId",
            "periodEndAt",
            "canceledAt",
            "accessUntil",
            "resubscribeUrl",
        ),
        optional_template_args=(
            "recipientName",
            "planName",
            "productName",
            "subscriptionManageUrl",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="subscription_plan_upgrade_receipt",
        required_template_args=(
            "subscriptionId",
            "invoiceId",
            "paymentId",
            "fromPlanName",
            "toPlanName",
            "amount",
            "currency",
            "changedAt",
            "receiptUrl",
        ),
        optional_template_args=(
            "recipientName",
            "effectiveAt",
            "paymentMethodSummary",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="payment_cancel_completed",
        required_template_args=("paymentId", "cancelAmount", "currency", "canceledAt"),
        optional_template_args=(
            "recipientName",
            "invoiceId",
            "orderName",
            "cancelReason",
            "receiptUrl",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="subscription_adjustment_completed",
        required_template_args=(
            "subscriptionId",
            "adjustmentType",
            "status",
            "adjustedAt",
        ),
        optional_template_args=(
            "recipientName",
            "previousStatus",
            "nextBillingAt",
            "accessUntil",
            "reasonSummary",
            "subscriptionManageUrl",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
    NotificationTemplateSpec(
        event_type="one_time_payment_paid",
        required_template_args=(
            "checkoutId",
            "paymentId",
            "orderName",
            "amount",
            "currency",
            "paidAt",
            "receiptUrl",
        ),
        optional_template_args=(
            "recipientName",
            "itemSummary",
            "paymentMethodSummary",
            "supportUrl",
        ),
        encrypted_args=(),
    ),
)

_SPECS_BY_EVENT = {spec.event_type: spec for spec in NOTIFICATION_TEMPLATE_SPECS}


def get_notification_template_spec(event_type: str) -> NotificationTemplateSpec | None:
    return _SPECS_BY_EVENT.get(event_type)


def encrypted_template_args_for_event(event_type: str) -> tuple[str, ...]:
    spec = get_notification_template_spec(event_type)
    return spec.encrypted_args if spec is not None else ()


def build_seed_notification_templates(now: datetime) -> list[NotificationTemplate]:
    return [
        NotificationTemplate(
            id=NotificationTemplate.generate_id(),
            template_key=f"default.{spec.event_type}",
            version=1,
            event_type=spec.event_type,
            product_code=None,
            product_type=None,
            status="active",
            subject_template=_seed_subject(spec),
            html_template=_seed_body(spec, html=True),
            text_template=_seed_body(spec, html=False),
            required_template_args=list(spec.required_template_args),
            created_at=now,
            updated_at=now,
        )
        for spec in NOTIFICATION_TEMPLATE_SPECS
    ]


def _seed_subject(spec: NotificationTemplateSpec) -> str:
    first_arg = spec.required_template_args[0]
    return f"[Payment] {spec.event_type} {{{{ {first_arg} }}}}"


def _seed_body(spec: NotificationTemplateSpec, *, html: bool) -> str:
    lines = [
        "{% set displayName = recipientName|default('고객님') %}",
        "안녕하세요 {{ displayName }}.",
        f"{spec.event_type} 알림입니다.",
    ]
    for arg in spec.required_template_args:
        lines.append(f"{arg}: {{{{ {arg} }}}}")
    if html:
        return "<br>".join(lines)
    return "\n".join(lines)
