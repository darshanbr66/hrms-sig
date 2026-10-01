"""Sending one email over SMTP (the worker's outbox dispatcher is the only caller).

Uses the standard library's `smtplib` in a worker thread with a timeout. With `starttls` the
connection must upgrade to TLS or the send fails; with `tls` it is TLS from the start;
`none` exists for the local mail catcher and is refused outside local and test
(`WorkerSettings`). Failures are reported as `EmailDeliveryError` carrying only the
exception's class name: SMTP server replies can echo addresses, which must not reach logs or
the outbox row.
"""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Final, Protocol

from pydantic import SecretStr

from app.platform.config import SmtpSecurity

SMTP_TIMEOUT_SECONDS: Final = 20


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
