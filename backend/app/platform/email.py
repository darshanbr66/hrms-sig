"""Sending one email (the worker's outbox dispatcher and the audit witness are the callers).

Two transports (`EMAIL_TRANSPORT`, ADR-030):

- **Resend** (`ResendSender`), the transport of every deployed environment: one HTTPS request
  to Resend's API (`POST /emails`) with the API key as a bearer token, over verified TLS, with a
  timeout. HTTPS works where outbound SMTP ports are blocked (Render's free instances).
- **SMTP** (`SmtpSender`), for the local mail catcher and tests only: the standard library's
  `smtplib` in a worker thread with a timeout. With `starttls` the connection must upgrade to
  TLS or the send fails; with `tls` it is TLS from the start; `none` is plain.

Failures are reported as `EmailDeliveryError` carrying only an exception class name or an HTTP
status: server replies can echo addresses, which must not reach logs or the outbox row. No
transport retries by itself; the outbox retries with backoff, so delivery is at least once.
"""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Final, Protocol

import httpx
from pydantic import SecretStr

from app.platform.config import SmtpSecurity

SMTP_TIMEOUT_SECONDS: Final = 20
RESEND_TIMEOUT_SECONDS: Final = 20
RESEND_EMAILS_URL: Final = "https://api.resend.com/emails"


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str


class EmailDeliveryError(Exception):
    """Sending failed; `reason` is an exception class name, never server text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmailSender(Protocol):
    async def send(self, email: OutgoingEmail) -> None: ...


class SmtpSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        security: SmtpSecurity,
        username: str | None = None,
        password: SecretStr | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._security = security
        self._username = username
        self._password = password

    async def send(self, email: OutgoingEmail) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = email.to
        message["Subject"] = email.subject
        message["Date"] = formatdate(usegmt=True)
        message["Message-ID"] = make_msgid(domain=self._sender.rpartition("@")[2] or None)
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(email.body)
        try:
            await asyncio.to_thread(self._deliver, message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise EmailDeliveryError(type(exc).__name__) from None

    def _deliver(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self._security is SmtpSecurity.TLS:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS, context=context
            )
        else:
            client = smtplib.SMTP(self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS)
        with client:
            if self._security is SmtpSecurity.STARTTLS:
                client.starttls(context=context)
            if self._username is not None and self._password is not None:
                client.login(self._username, self._password.get_secret_value())
            client.send_message(message)


class ResendSender:
    """Resend's HTTPS API. The HTTP client is the caller's (closed by whoever created it)."""

    def __init__(self, *, client: httpx.AsyncClient, api_key: SecretStr, sender: str) -> None:
        self._client = client
        self._api_key = api_key
        self._sender = sender

    async def send(self, email: OutgoingEmail) -> None:
        try:
            response = await self._client.post(
                RESEND_EMAILS_URL,
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                json={
                    "from": self._sender,
                    "to": [email.to],
                    "subject": email.subject,
                    "text": email.body,
                    "headers": {"Auto-Submitted": "auto-generated"},
                },
                timeout=RESEND_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(type(exc).__name__) from None
        if not response.is_success:
            # The body can name the recipient or the sending domain; only the status is kept.
            raise EmailDeliveryError(f"ResendHttp{response.status_code}")
