"""The identity module's interface for other modules (docs/architecture.md §4).

- `authenticate` resolves an access token to its session on every request: the token must be
  unexpired, the session open (not revoked, within its idle and absolute limits) and the
  account active. User activity extends the idle limit; background requests do not
  (docs/security-architecture.md §3.5).
- `rotate_user_sessions` ends the outstanding access tokens of every session of a user, so
  each session must refresh, which issues new tokens. Used when the user's roles change.
- `user_status` lets other modules check an account exists without reading its secrets;
  `active_user_ids`, `active_email` and `emails_of` let them address and name accounts
  (work email is `public_internal`) without reading anything else.
- `create_invited_account` and `issue_invite` start the account lifecycle: an `invited`
  account and its 72-hour, single-use invite token (docs/security-architecture.md §3.1).
- `accounts_of_employees` names the account of each of a set of employee records, for lists.
- `linked_account` and `disable_linked_account` end it when the employee leaves: the account
  linked to the record is disabled and every session revoked in the caller's transaction
  (product-requirements MVP item 11). The caller decides whether it may, and audits it.
- `erase_account` is the identity part of the permanent erasure of one person's data (the
  erasure module, docs/database-design.md §9.1).
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.modules.identity import accounts, erasure
from app.modules.identity import repository as repo
from app.modules.identity.defaults import ACTIVITY_WRITE_INTERVAL
from app.modules.identity.models import AuthSession, OneTimePurpose, OneTimeToken, User, UserStatus
from app.platform import settings
from app.platform.authz.context import SessionScope
from app.platform.db import ID, TENANT_FIELD, Database, Session, truncate
from app.platform.erasure import Erased
from app.platform.security import tokens
from app.platform.security.emails import canonical_email

# docs/security-architecture.md §3.5. `__Host-` pins the access cookie to this origin and
# path `/`; the refresh cookie is sent only to the refresh endpoint.
ACCESS_COOKIE: Final = "__Host-sv_at"
REFRESH_COOKIE: Final = "__Secure-sv_rt"
REFRESH_COOKIE_PATH: Final = "/api/v1/auth/refresh"
# Sent by the SPA on background polling, which must not keep an idle session alive.
BACKGROUND_HEADER: Final = "x-sv-background"


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    scope: SessionScope
    employee_id: uuid.UUID | None
    step_up_at: datetime | None
    # Due activity: the new last-activity time and idle expiry, written by `touch_session`.
    activity: tuple[datetime, datetime] | None = None


async def authenticate(
    session: Session, access_token: str, now: datetime, *, user_activity: bool
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
        # An account waiting to re-enrol after an MFA reset has only enrolment sessions (the
        # database refuses a full one too).
        or (user.mfa_reenrolment_required and auth.scope != SessionScope.MFA_ENROLMENT.value)
    ):
        return None
    if user.organization_id is None or auth.organization_id != user.organization_id:
        return None
    activity = None
    if user_activity and now - auth.last_activity_at >= ACTIVITY_WRITE_INTERVAL:
        # Each session keeps the idle window it was created with, so a change to the
        # idle-timeout setting applies to new sessions without a settings read per request.
        idle_window = auth.idle_expires_at - auth.last_activity_at
        activity = (now, repo.session_expiry(now, auth.absolute_expires_at, idle_window))
    return AuthenticatedSession(
        user.organization_id,
        user.id,
        auth.id,
        SessionScope(auth.scope),
        user.employee_id,
        auth.step_up_at,
        activity,
    )


async def touch_session(database: Database, auth: AuthenticatedSession) -> None:
    """Extend an active session's idle window after user activity: one atomic update outside any
    transaction, so concurrent requests of one session never collide on it (as they would in a
    transaction) and the times only ever move forward (`$max`). A revoked session is left alone."""
    if auth.activity is None:
        return
    last_activity_at, idle_expires_at = auth.activity
    await database.collection(AuthSession.__collection__).update_one(
        {ID: auth.session_id, TENANT_FIELD: auth.organization_id, "revoked_at": None},
        {
            "$max": {
                "last_activity_at": truncate(last_activity_at),
                "idle_expires_at": truncate(idle_expires_at),
            }
        },
    )


async def rotate_user_sessions(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    # Refresh tokens stay valid: the next refresh is how each session gets its new tokens.
    await repo.expire_tokens(session, now=now, user_id=user_id, retire_refresh=False)


async def user_status(session: Session, user_id: uuid.UUID) -> UserStatus | None:
    user = await session.get(User, user_id)
    return UserStatus(user.status) if user is not None else None


async def active_user_ids(session: Session, user_ids: Collection[uuid.UUID]) -> list[uuid.UUID]:
    """The given accounts that are active, in ID order."""
    return await repo.active_user_ids(session, user_ids)


async def account_for_employee(session: Session, employee_id: uuid.UUID) -> uuid.UUID | None:
    """The active account linked to an employee record, for notifying them; None otherwise."""
    user = await repo.user_for_employee(session, employee_id)
    return user.id if user is not None and user.status == UserStatus.ACTIVE else None


async def active_email(session: Session, user_id: uuid.UUID) -> str | None:
    """The address of an active account, for an email the worker is about to send."""
    user = await session.get(User, user_id)
    return user.email if user is not None and user.status == UserStatus.ACTIVE else None


async def emails_of(session: Session, user_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    return {user.id: user.email for user in await session.find(User, {"id": {"$in": list(user_ids)}})}


async def create_invited_account(
    session: Session, email: str, *, employee_id: uuid.UUID | None = None
) -> uuid.UUID:
    user = User(email=canonical_email(email), status=UserStatus.INVITED.value, employee_id=employee_id)
    session.add(user)
    await session.flush()
    return user.id


async def email_in_use(session: Session, email: str) -> bool:
    """Whether any organization has an account with this address (unique across all of them)."""
    return await repo.email_in_use(session, email)


async def issue_invite(session: Session, user_id: uuid.UUID, now: datetime) -> str:
    """A new invite token for an invited account; earlier ones stop working. Its lifetime is
    the `security.invite.ttl_hours` setting."""
    ttl = timedelta(hours=(await settings.load(session))[settings.INVITE_TTL])
    raw = tokens.new_token()
    await repo.retire_one_time_tokens(session, user_id, OneTimePurpose.INVITE, now)
    session.add(
        OneTimeToken(
            user_id=user_id,
            purpose=OneTimePurpose.INVITE.value,
            token_hash=tokens.token_digest(raw),
            expires_at=now + ttl,
        )
    )
    return raw


@dataclass(frozen=True, slots=True)
class LinkedAccount:
    user_id: uuid.UUID
    status: UserStatus


@dataclass(frozen=True, slots=True)
class EmployeeAccount:
    user_id: uuid.UUID
    # Work email (`public_internal`).
    email: str
    status: UserStatus


async def accounts_of_employees(
    session: Session, employee_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, EmployeeAccount]:
    """The account linked to each employee record that has one (at most one each), for lists
    of people that show their account (the former employees list)."""
    if not employee_ids:
        return {}
    users = await session.find(User, {"employee_id": {"$in": sorted(employee_ids)}})
    return {
        user.employee_id: EmployeeAccount(user.id, user.email, UserStatus(user.status))
        for user in users
        if user.employee_id is not None
    }


async def linked_account(session: Session, employee_id: uuid.UUID) -> LinkedAccount | None:
    """The account linked to an employee record, whatever its status."""
    user = await repo.user_for_employee(session, employee_id)
    return LinkedAccount(user.id, UserStatus(user.status)) if user is not None else None


async def disable_linked_account(session: Session, user_id: uuid.UUID, now: datetime) -> int | None:
    """Disable the account and revoke every session; the number of sessions revoked, or None
    when it was already disabled."""
    user = await repo.lock_user(session, user_id)
    if user is None or user.status == UserStatus.DISABLED:
        return None
    return len(await accounts.disable(session, user, now))


async def erase_account(
    session: Session, employee_id: uuid.UUID, now: datetime
) -> tuple[uuid.UUID | None, Erased]:
    """Erase the account linked to an employee record, in the caller's transaction."""
    return await erasure.erase_account(session, employee_id, now)
