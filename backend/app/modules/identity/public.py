"""The identity module's interface for other modules (docs/architecture.md §4).

- `authenticate` resolves an access token to its session on every request: the token must be
  unexpired, the session open (not revoked, within its idle and absolute limits) and the
  account active. User activity extends the idle limit; background requests do not
  (docs/security-architecture.md §3.5).
- `rotate_user_sessions` ends the outstanding access tokens of every session of a user, so
  each session must refresh, which issues new tokens. Used when the user's roles change.
- `user_status` lets other modules check an account exists without reading its secrets.
- `create_invited_account` and `issue_invite` start the account lifecycle: an `invited`
  account and its 72-hour, single-use invite token (docs/security-architecture.md §3.1).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import repository as repo
from app.modules.identity.defaults import ACTIVITY_WRITE_INTERVAL, INVITE_TTL, SESSION_IDLE_TIMEOUT
from app.modules.identity.models import OneTimePurpose, OneTimeToken, User, UserStatus
from app.platform.authz.context import SessionScope
from app.platform.security import tokens

# docs/security-architecture.md §3.5. `__Host-` pins the access cookie to this origin and
# path `/`; the refresh cookie is sent only to the refresh endpoint.
ACCESS_COOKIE: Final = "__Host-sv_at"
REFRESH_COOKIE: Final = "__Secure-sv_rt"
REFRESH_COOKIE_PATH: Final = "/api/v1/auth/refresh"
# Sent by the SPA on background polling, which must not keep an idle session alive.
BACKGROUND_HEADER: Final = "x-sv-background"


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    user_id: uuid.UUID
    session_id: uuid.UUID
    scope: SessionScope
    employee_id: uuid.UUID | None
    step_up_at: datetime | None


async def authenticate(
    session: AsyncSession, access_token: str, now: datetime, *, user_activity: bool
) -> AuthenticatedSession | None:
    if not tokens.looks_like_token(access_token):
        return None
    record = await repo.access_record(session, tokens.token_digest(access_token))
    if record is None:
        return None
    auth, user = record.session, record.user
    if (
        record.token_expires_at <= now
        or auth.revoked_at is not None
        or auth.idle_expires_at <= now
        or auth.absolute_expires_at <= now
        or user.status != UserStatus.ACTIVE
    ):
        return None
    if user_activity and now - auth.last_activity_at >= ACTIVITY_WRITE_INTERVAL:
        auth.last_activity_at = now
        auth.idle_expires_at = repo.session_expiry(now, auth.absolute_expires_at, SESSION_IDLE_TIMEOUT)
    return AuthenticatedSession(user.id, auth.id, SessionScope(auth.scope), user.employee_id, auth.step_up_at)


async def rotate_user_sessions(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    # Refresh tokens stay valid: the next refresh is how each session gets its new tokens.
    await repo.expire_tokens(session, now=now, user_id=user_id, retire_refresh=False)


async def user_status(session: AsyncSession, user_id: uuid.UUID) -> UserStatus | None:
    status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one_or_none()
    return UserStatus(status) if status is not None else None


async def create_invited_account(
    session: AsyncSession, email: str, *, employee_id: uuid.UUID | None = None
) -> uuid.UUID:
    user = User(email=email, status=UserStatus.INVITED.value, employee_id=employee_id)
    session.add(user)
    await session.flush()
    return user.id


async def email_in_use(session: AsyncSession, email: str) -> bool:
    return await repo.user_by_email(session, email) is not None


async def issue_invite(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> str:
    """A new invite token for an invited account; earlier ones stop working."""
    raw = tokens.new_token()
    await repo.retire_one_time_tokens(session, user_id, OneTimePurpose.INVITE, now)
    session.add(
        OneTimeToken(
            user_id=user_id,
            purpose=OneTimePurpose.INVITE.value,
            token_hash=tokens.token_digest(raw),
            expires_at=now + INVITE_TTL,
        )
    )
    return raw
