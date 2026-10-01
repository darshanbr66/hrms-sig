"""Security emails queued by identity flows (docs/security-architecture.md §3).

Each function writes an outbox row in the caller's transaction (notify.public.enqueue), so an
email exists exactly when the change that caused it commits. The idempotency keys make each
event produce one email: one per lock, one per new device, one per reset request.

Template data is display text only: never a token, a code, an address or a password.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notify.public import EmailTemplate, cancel_pending, enqueue

# Bytes of a user agent kept for display in an email (the full string is in the session).
_BROWSER_CHARS = 120


_SAFE_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;()/_-+")


def browser_label(user_agent: str | None) -> str:
    """A short description of the browser for an email. The user agent is chosen by whoever
    signed in, so only plain characters are kept and nothing can read as a link or an address."""
    if not user_agent:
        return "an unknown browser"
    kept = "".join(ch if ch in _SAFE_CHARACTERS else " " for ch in user_agent)
    while "//" in kept:
        kept = kept.replace("//", "/")
    label = " ".join(kept.split())[:_BROWSER_CHARS]
    return label or "an unknown browser"


async def invite(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    """Queue an invite; any invite still waiting to be sent is cancelled (only the newest link works)."""
    await cancel_pending(session, user_id=user_id, templates=[EmailTemplate.INVITE], now=now)
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.INVITE,
        idempotency_key=f"invite:{uuid.uuid4()}",
        now=now,
    )


async def password_reset(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    await cancel_pending(session, user_id=user_id, templates=[EmailTemplate.PASSWORD_RESET], now=now)
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.PASSWORD_RESET,
        idempotency_key=f"password_reset:{uuid.uuid4()}",
        now=now,
    )


async def password_changed(
    session: AsyncSession, user_id: uuid.UUID, now: datetime, *, by_reset: bool
) -> None:
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.PASSWORD_CHANGED,
        idempotency_key=f"password_changed:{uuid.uuid4()}",
        now=now,
        data={"by_reset": by_reset},
    )


async def new_device(
    session: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID, user_agent: str | None, now: datetime
) -> None:
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.NEW_DEVICE,
        idempotency_key=f"new_device:{device_id}",
        now=now,
        data={"browser": browser_label(user_agent), "signed_in_at": now.isoformat(timespec="minutes")},
    )


async def account_locked(
    session: AsyncSession, user_id: uuid.UUID, locked_until: datetime, minutes: int, now: datetime
) -> None:
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.ACCOUNT_LOCKED,
        idempotency_key=f"account_locked:{user_id}:{locked_until.isoformat()}",
        now=now,
        data={"locked_minutes": minutes},
    )


async def recovery_code_used(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, remaining: int, now: datetime
) -> None:
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.RECOVERY_CODE_USED,
        idempotency_key=f"recovery_code_used:{session_id}",
        now=now,
        data={"remaining_codes": remaining},
    )


async def mfa_changed(session: AsyncSession, user_id: uuid.UUID, change: str, now: datetime) -> None:
    """`change` is one of: authenticator_added, authenticator_removed, recovery_codes_replaced."""
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.MFA_CHANGED,
        idempotency_key=f"mfa_changed:{uuid.uuid4()}",
        now=now,
        data={"change": change},
    )


async def cancel_invites(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    await cancel_pending(session, user_id=user_id, templates=[EmailTemplate.INVITE], now=now)
