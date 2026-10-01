"""Data access for accounts, credentials, factors, sessions and tokens.

Lookups use the unique indexes (email, token digests) or the account's primary key. Counters
that concurrent requests can race on (failed sign-ins, one-time token attempts) change in a
single UPDATE ... RETURNING, so no update is lost. Changes that must see a consistent set of
an account's factors or sessions lock the account row first (`lock_user`).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.defaults import FAILURE_WINDOW, LOCK_AFTER_FAILURES, LOCK_DURATION
from app.modules.identity.models import (
    AuthSession,
    Credential,
    MfaFactor,
    OneTimePurpose,
    OneTimeToken,
    RecoveryCode,
    RevokedReason,
    SessionToken,
    TokenKind,
    User,
)

# --- accounts --------------------------------------------------------------------------


async def user_by_email(session: AsyncSession, email: str) -> User | None:
    return (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def lock_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """The account row, locked for the rest of the transaction."""
    query = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    return (await session.execute(query)).scalar_one_or_none()


async def credential(session: AsyncSession, user_id: uuid.UUID) -> Credential | None:
    return await session.get(Credential, user_id)


@dataclass(frozen=True, slots=True)
class FailureState:
    count: int
    locked_until: datetime | None


async def register_failure(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> FailureState:
    """Count a failed sign-in attempt in the current window; lock at the threshold."""
    window_expired = or_(
        User.failed_login_window_started_at.is_(None),
        User.failed_login_window_started_at <= now - FAILURE_WINDOW,
    )
    new_count = case((window_expired, 1), else_=User.failed_login_count + 1)
    statement = (
        update(User)
        .where(User.id == user_id)
        .values(
            failed_login_count=new_count,
            failed_login_window_started_at=case(
                (window_expired, now), else_=User.failed_login_window_started_at
            ),
            last_failed_login_at=now,
            locked_until=case(
                (new_count >= LOCK_AFTER_FAILURES, now + LOCK_DURATION), else_=User.locked_until
            ),
        )
        .returning(User.failed_login_count, User.locked_until)
    )
    row = (await session.execute(statement)).one()
    return FailureState(row.failed_login_count, row.locked_until)


async def clear_failures(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            failed_login_count=0,
            failed_login_window_started_at=None,
            locked_until=None,
            last_login_at=now,
            updated_at=now,
        )
    )


@dataclass(frozen=True, slots=True)
class UserPage:
    users: Sequence[User]
    next_cursor: uuid.UUID | None


async def list_users(session: AsyncSession, *, after: uuid.UUID | None, limit: int) -> UserPage:
    """Accounts in creation order (UUIDv7 IDs are time-ordered), keyset-paginated."""
    query = select(User).order_by(User.id).limit(limit + 1)
    if after is not None:
        query = query.where(User.id > after)
    rows = list((await session.execute(query)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return UserPage(rows, rows[-1].id if has_more and rows else None)


# --- factors and recovery codes ---------------------------------------------------------


async def confirmed_factors(session: AsyncSession, user_id: uuid.UUID) -> Sequence[MfaFactor]:
    query = (
        select(MfaFactor)
        .where(
            MfaFactor.user_id == user_id, MfaFactor.confirmed_at.is_not(None), MfaFactor.revoked_at.is_(None)
        )
        .order_by(MfaFactor.id)
    )
    return list((await session.execute(query)).scalars())


async def pending_factor(
    session: AsyncSession, user_id: uuid.UUID, factor_id: uuid.UUID, created_after: datetime
) -> MfaFactor | None:
    query = select(MfaFactor).where(
        MfaFactor.id == factor_id,
        MfaFactor.user_id == user_id,
        MfaFactor.confirmed_at.is_(None),
        MfaFactor.revoked_at.is_(None),
        MfaFactor.created_at > created_after,
    )
    return (await session.execute(query)).scalar_one_or_none()


async def revoke_pending_factors(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(MfaFactor)
        .where(MfaFactor.user_id == user_id, MfaFactor.confirmed_at.is_(None), MfaFactor.revoked_at.is_(None))
        .values(revoked_at=now, updated_at=now)
    )


async def record_factor_use(session: AsyncSession, factor_id: uuid.UUID, step: int, now: datetime) -> bool:
    """Accept a TOTP step only if it is later than the last one used (replay protection).
    Atomic, so two requests with the same code cannot both succeed."""
    statement = (
        update(MfaFactor)
        .where(
            MfaFactor.id == factor_id,
            or_(MfaFactor.last_used_step.is_(None), MfaFactor.last_used_step < step),
        )
        .values(last_used_step=step, last_used_at=now, updated_at=now)
        .returning(MfaFactor.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def use_recovery_code(session: AsyncSession, user_id: uuid.UUID, digest: bytes, now: datetime) -> bool:
    statement = (
        update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_hash == digest,
            RecoveryCode.used_at.is_(None),
            RecoveryCode.revoked_at.is_(None),
        )
        .values(used_at=now)
        .returning(RecoveryCode.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def replace_recovery_codes(
    session: AsyncSession, user_id: uuid.UUID, digests: Sequence[bytes], now: datetime
) -> None:
    await session.execute(
        update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None), RecoveryCode.revoked_at.is_(None)
        )
        .values(revoked_at=now)
    )
    session.add_all(RecoveryCode(user_id=user_id, code_hash=digest) for digest in digests)


async def unused_recovery_code_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    query = select(func.count()).where(
        RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None), RecoveryCode.revoked_at.is_(None)
    )
    return int((await session.execute(query)).scalar_one())


# --- one-time tokens --------------------------------------------------------------------


async def lock_one_time_token(
    session: AsyncSession, digest: bytes, purpose: OneTimePurpose, now: datetime
) -> OneTimeToken | None:
    """A usable token (unused, unexpired), locked for the rest of the transaction."""
    query = (
        select(OneTimeToken)
        .where(
            OneTimeToken.token_hash == digest,
            OneTimeToken.purpose == purpose.value,
            OneTimeToken.used_at.is_(None),
            OneTimeToken.expires_at > now,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def find_one_time_token(
    session: AsyncSession, digest: bytes, purpose: OneTimePurpose, now: datetime
) -> OneTimeToken | None:
    query = select(OneTimeToken).where(
        OneTimeToken.token_hash == digest,
        OneTimeToken.purpose == purpose.value,
        OneTimeToken.used_at.is_(None),
        OneTimeToken.expires_at > now,
    )
    return (await session.execute(query)).scalar_one_or_none()


async def retire_one_time_tokens(
    session: AsyncSession, user_id: uuid.UUID, purpose: OneTimePurpose, now: datetime
) -> None:
    """Mark every outstanding token of a purpose used, so only the newest one works."""
    await session.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.user_id == user_id,
            OneTimeToken.purpose == purpose.value,
            OneTimeToken.used_at.is_(None),
        )
        .values(used_at=now)
    )


# --- sessions and session tokens --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessRecord:
    session: AuthSession
    user: User
    token_expires_at: datetime


async def access_record(session: AsyncSession, digest: bytes) -> AccessRecord | None:
    query = (
        select(SessionToken.expires_at, AuthSession, User)
        .join(AuthSession, AuthSession.id == SessionToken.session_id)
        .join(User, User.id == AuthSession.user_id)
        .where(SessionToken.token_hash == digest, SessionToken.kind == TokenKind.ACCESS.value)
    )
    row = (await session.execute(query)).one_or_none()
    return AccessRecord(row.AuthSession, row.User, row.expires_at) if row else None


async def find_refresh_token(session: AsyncSession, digest: bytes) -> SessionToken | None:
    query = select(SessionToken).where(
        SessionToken.token_hash == digest, SessionToken.kind == TokenKind.REFRESH.value
    )
    return (await session.execute(query)).scalar_one_or_none()


async def lock_refresh_token(session: AsyncSession, digest: bytes) -> SessionToken | None:
    query = (
        select(SessionToken)
        .where(SessionToken.token_hash == digest, SessionToken.kind == TokenKind.REFRESH.value)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def expire_tokens(
    session: AsyncSession,
    *,
    now: datetime,
    session_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    retire_refresh: bool = True,
) -> None:
    """End the outstanding access tokens of one session or of every session of a user, and
    (unless `retire_refresh` is false) retire their unused refresh tokens."""
    sessions = select(AuthSession.id)
    if session_id is not None:
        sessions = sessions.where(AuthSession.id == session_id)
    if user_id is not None:
        sessions = sessions.where(AuthSession.user_id == user_id)
    in_scope = SessionToken.session_id.in_(sessions.scalar_subquery())
    await session.execute(
        update(SessionToken)
        .where(in_scope, SessionToken.kind == TokenKind.ACCESS.value, SessionToken.expires_at > now)
        .values(expires_at=now)
    )
    if retire_refresh:
        await session.execute(
            update(SessionToken)
            .where(in_scope, SessionToken.kind == TokenKind.REFRESH.value, SessionToken.used_at.is_(None))
            .values(used_at=now)
        )


async def active_sessions(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> Sequence[AuthSession]:
    query = (
        select(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.idle_expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
        .order_by(AuthSession.last_activity_at.desc())
    )
    return list((await session.execute(query)).scalars())


async def revoke_sessions(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    reason: RevokedReason,
    now: datetime,
    session_id: uuid.UUID | None = None,
    except_session_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Revoke one or all of a user's open sessions; returns the IDs revoked."""
    statement = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if session_id is not None:
        statement = statement.where(AuthSession.id == session_id)
    if except_session_id is not None:
        statement = statement.where(AuthSession.id != except_session_id)
    revoked = list(
        (
            await session.execute(
                statement.values(revoked_at=now, revoked_reason=reason.value).returning(AuthSession.id)
            )
        ).scalars()
    )
    for revoked_id in revoked:
        await expire_tokens(session, now=now, session_id=revoked_id)
    return revoked


def session_expiry(now: datetime, absolute_expires_at: datetime, idle: timedelta) -> datetime:
    return min(now + idle, absolute_expires_at)
