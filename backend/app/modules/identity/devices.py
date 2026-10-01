"""Trusted devices (docs/security-architecture.md §3.4, "Successful login from a new device").

A browser is recognized by a random 256-bit cookie (`__Host-sv_dev`), stored only as its
SHA-256 digest. Recognition decides one thing: whether a completed sign-in is reported to the
user as from a new device. It never replaces the second factor, never skips step-up, and
does not authenticate anything (§3.3: no "remember this device").

- Trust is created at the end of a successful sign-in (both factors passed) or of invite
  activation, and expires after `security.trusted_device.lifetime_days`; use does not
  extend it.
- Trust is revoked by the user, by an administrator revoking the user's sessions, by
  disabling the account, by a password change or reset, and by any MFA change. A revoked or
  expired device is reported as new at its next sign-in.
- Only the browser's user agent and the times are kept; no IP address, no fingerprinting.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import repository as repo
from app.modules.identity.models import DeviceRevokedReason, TrustedDevice
from app.platform.security import tokens

DEVICE_COOKIE: Final = "__Host-sv_dev"


@dataclass(frozen=True, slots=True)
class IssuedDevice:
    """A new device cookie to set on the response."""

    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Recognition:
    device_id: uuid.UUID
    new: bool
    issued: IssuedDevice | None


def digest_of(device_token: str | None) -> bytes | None:
    if device_token is None or not tokens.looks_like_token(device_token):
        return None
    return tokens.token_digest(device_token)


async def recognize(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    device_token: str | None,
    user_agent: str | None,
    now: datetime,
    lifetime: timedelta,
) -> Recognition:
    """The trusted device behind this cookie, or a newly trusted one."""
    digest = digest_of(device_token)
    if digest is not None:
        device = await repo.lock_trusted_device(session, user_id, digest, now)
        if device is not None:
            device.last_seen_at = now
            return Recognition(device.id, new=False, issued=None)
    raw = tokens.new_token()
    device = TrustedDevice(
        user_id=user_id,
        token_hash=tokens.token_digest(raw),
        user_agent=user_agent,
        last_seen_at=now,
        expires_at=now + lifetime,
    )
    session.add(device)
    await session.flush()
    return Recognition(device.id, new=True, issued=IssuedDevice(raw, device.expires_at))


async def revoke_all(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    reason: DeviceRevokedReason,
    now: datetime,
    keep_token: str | None = None,
) -> int:
    """Revoke a user's devices, except the one presenting `keep_token` (the browser making the
    change, which has just proved itself), if given."""
    return await repo.revoke_devices(
        session, user_id=user_id, reason=reason.value, now=now, except_digest=digest_of(keep_token)
    )
