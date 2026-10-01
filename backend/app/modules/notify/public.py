"""The notify module's interface (docs/architecture.md §4).

`enqueue` writes an outbox row in the caller's transaction, so the email exists exactly when
the change that caused it commits, and never otherwise. Nothing is sent here: the worker's
dispatcher sends it (`dispatcher.py`).

- The row names the recipient account and a template. It never holds an address, a token or
  another secret: the template's renderer resolves the address and creates any link token at
  send time, so a secret exists only in the email itself and as a hash.
- `idempotency_key` is unique: enqueueing the same event twice keeps one email (for example
  one lockout email per lock, however many failed attempts follow).
- `template_data` holds small, non-sensitive display values only, and is checked.
"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notify.models import EmailOutbox, EmailTemplate, OutboxStatus
from app.platform.email import OutgoingEmail
from app.platform.logging import is_sensitive_key

MAX_DATA_BYTES: Final = 2048
MAX_TEXT: Final = 300
_IDENTIFIER: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")
_EMAIL_SHAPE: Final = re.compile(r"[^@\s]+@[^@\s]+")
# Recipients are resolved at send time; template data never names contact details.
_CONTACT_PARTS: Final = frozenset({"email", "phone", "mobile", "address"})

type DataValue = str | int | bool | None

__all__ = ["EmailTemplate", "OutboxItem", "Renderer", "cancel_pending", "enqueue"]


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """What a template renderer receives: the queued event, never an address or a secret."""

    id: uuid.UUID
    user_id: uuid.UUID
    template: EmailTemplate
    data: Mapping[str, DataValue]
    created_at: datetime


# A renderer builds the email at send time, inside a transaction it may write to (for example
# to create the link token whose hash it stores). It returns None when the email no longer
# applies (the account was disabled, the invite was already accepted); the row is cancelled.
type Renderer = Callable[[AsyncSession, OutboxItem], Awaitable[OutgoingEmail | None]]


def _check_data(data: Mapping[str, DataValue]) -> dict[str, DataValue]:
    checked: dict[str, DataValue] = {}
    for key, value in data.items():
        if (
            not _IDENTIFIER.fullmatch(key)
            or is_sensitive_key(key)
            or not _CONTACT_PARTS.isdisjoint(key.split("_"))
        ):
            raise ValueError(f"template data key {key!r} is not allowed")
        if isinstance(value, str) and (
            len(value) > MAX_TEXT or _EMAIL_SHAPE.search(value) or "\x00" in value
        ):
            raise ValueError(f"template data {key!r} must be short display text without addresses")
        if not (value is None or isinstance(value, str | int | bool)):
            raise ValueError(f"template data {key!r} must be a string, number, boolean or null")
        checked[key] = value
    if len(json.dumps(checked).encode()) > MAX_DATA_BYTES:
        raise ValueError("template data is too large")
    return checked


async def enqueue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    template: EmailTemplate,
    idempotency_key: str,
    now: datetime,
    data: Mapping[str, DataValue] | None = None,
) -> bool:
    """Queue an email in the caller's transaction; False if this event was already queued."""
    if not 1 <= len(idempotency_key) <= 200:
        raise ValueError("idempotency_key must be 1 to 200 characters")
    statement = (
        insert(EmailOutbox)
        .values(
            user_id=user_id,
            template=template.value,
            template_data=_check_data(data) if data else None,
            idempotency_key=idempotency_key,
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(index_elements=[EmailOutbox.idempotency_key])
        .returning(EmailOutbox.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def cancel_pending(
    session: AsyncSession, *, user_id: uuid.UUID, templates: Collection[EmailTemplate], now: datetime
) -> int:
    """Cancel queued, not yet claimed emails (for example an invite that was resent or revoked).
    An email already being sent is not stopped; its renderer re-checks eligibility."""
    statement = (
        update(EmailOutbox)
        .where(
            EmailOutbox.user_id == user_id,
            EmailOutbox.template.in_([template.value for template in templates]),
            EmailOutbox.status == OutboxStatus.PENDING.value,
        )
        .values(status=OutboxStatus.CANCELLED.value, updated_at=now)
        .returning(EmailOutbox.id)
    )
    return len((await session.execute(statement)).all())
