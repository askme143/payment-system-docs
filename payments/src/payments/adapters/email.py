from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid

from jinja2 import Environment, StrictUndefined, TemplateError, select_autoescape

from payments.application.ports.notifications import (
    EmailSendError,
    EmailSendResult,
    RenderedEmail,
    TemplateRenderError,
)
from payments.domain.entities.notification import NotificationTemplate, TemplateArgs


class JinjaTemplateRenderer:
    def render(
        self,
        *,
        template: NotificationTemplate,
        template_args: TemplateArgs,
    ) -> RenderedEmail:
        try:
            subject = _plain_environment().from_string(
                template.subject_template
            ).render(template_args)
            html_body = _html_environment().from_string(
                template.html_template
            ).render(template_args)
            text_body = _plain_environment().from_string(
                template.text_template
            ).render(template_args)
        except TemplateError as exc:
            raise TemplateRenderError("template render failed") from exc
        return RenderedEmail(
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )


@dataclass(frozen=True, slots=True)
class SMTPEmailSenderConfig:
    host: str
    port: int
    from_email: str
    from_name: str | None = None
    username: str | None = None
    password: str | None = None
    use_tls: bool = True
    timeout_seconds: float = 10.0
    reply_to: str | None = None


class SMTPEmailSender:
    def __init__(self, config: SMTPEmailSenderConfig) -> None:
        self._config = config

    async def send_email(
        self,
        *,
        recipient_email: str,
        subject: str,
        html_body: str,
        text_body: str,
    ) -> EmailSendResult:
        try:
            return await asyncio.to_thread(
                self._send_email_sync,
                recipient_email=recipient_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
            )
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailSendError(
                "recipient email was refused",
                code="invalid_recipient",
                retryable=False,
            ) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise EmailSendError(
                "sender email was refused",
                code="rejected_sender",
                retryable=False,
            ) from exc
        except smtplib.SMTPResponseException as exc:
            retryable = 400 <= exc.smtp_code < 500
            raise EmailSendError(
                "SMTP response error",
                code=f"smtp_{exc.smtp_code}",
                retryable=retryable,
            ) from exc
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            raise EmailSendError(
                "SMTP connection error",
                code="smtp_connection_error",
                retryable=True,
            ) from exc

    def _send_email_sync(
        self,
        *,
        recipient_email: str,
        subject: str,
        html_body: str,
        text_body: str,
    ) -> EmailSendResult:
        message = EmailMessage()
        message["Message-ID"] = make_msgid()
        message["Subject"] = subject
        message["From"] = _format_sender(
            self._config.from_email,
            self._config.from_name,
        )
        message["To"] = recipient_email
        if self._config.reply_to is not None:
            message["Reply-To"] = self._config.reply_to
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        with smtplib.SMTP(
            self._config.host,
            self._config.port,
            timeout=self._config.timeout_seconds,
        ) as smtp:
            if self._config.use_tls:
                smtp.starttls()
            if self._config.username is not None:
                smtp.login(self._config.username, self._config.password or "")
            response = smtp.send_message(message)
        if response:
            raise EmailSendError(
                "recipient email was refused",
                code="invalid_recipient",
                retryable=False,
            )
        message_id = message["Message-ID"] or ""
        return EmailSendResult(provider_message_id=message_id)


class RecordingEmailSender:
    def __init__(self) -> None:
        self.messages: list[RecordedEmailMessage] = []

    async def send_email(
        self,
        *,
        recipient_email: str,
        subject: str,
        html_body: str,
        text_body: str,
    ) -> EmailSendResult:
        message_id = f"recorded-{len(self.messages) + 1}"
        self.messages.append(
            RecordedEmailMessage(
                provider_message_id=message_id,
                recipient_email=recipient_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
            )
        )
        return EmailSendResult(provider_message_id=message_id)


@dataclass(frozen=True, slots=True)
class RecordedEmailMessage:
    provider_message_id: str
    recipient_email: str
    subject: str
    html_body: str
    text_body: str


def _plain_environment() -> Environment:
    return Environment(undefined=StrictUndefined, autoescape=False)


def _html_environment() -> Environment:
    return Environment(
        undefined=StrictUndefined,
        autoescape=select_autoescape(default=True, default_for_string=True),
    )


def _format_sender(email: str, name: str | None) -> str:
    if name is None:
        return email
    return f"{name} <{email}>"
