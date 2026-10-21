"""Data access for accounts, credentials, factors, sessions and tokens.

Lookups use the unique indexes (email, token digests) or the account's identifier. A sign-in
email or a token is looked up across organizations, and the unit of work is then bound to the
organization it belongs to (`_resolve`); everything after that runs inside it. Counters that
concurrent requests can race on (failed sign-ins, one-time token attempts, TOTP steps) change
in one atomic update, so no update is lost. Changes that must see a consistent set of an
account's factors or sessions lock the account document first (`lock_user`).
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

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
    TrustedDevice,
    User,
    UserStatus,
)
from app.platform.db import Record, Session, enter_tenant
from app.platform.security.emails import canonical_email


def _resolve[T: Record](session: Session, found: T | None) -> T | None:
    """Bind an unauthenticated unit of work, and the rest of its request, to the organization
    of what it found."""
    if found is not None and found.organization_id is not None:
        session.bind_tenant(found.organization_id)
        enter_tenant(found.organization_id)
    return found


# --- accounts --------------------------------------------------------------------------


async def user_by_email(session: Session, email: str) -> User | None:
    """The account with this email in any organization; the unit of work then belongs to it."""
    return _resolve(session, await session.find_any_tenant(User, {"email": canonical_email(email)}))


async def email_in_use(session: Session, email: str) -> bool:
    """Whether any organization has an account with this email (addresses are unique overall)."""
    return await session.exists_any_tenant(User, {"email": canonical_email(email)})


async def get_user(session: Session, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def user_for_employee(session: Session, employee_id: uuid.UUID) -> User | None:
    """The account linked to an employee record, if any (at most one, `uq_users_employee_id`)."""
    return await session.find_one(User, {"employee_id": employee_id})


async def lock_user(session: Session, user_id: uuid.UUID) -> User | None:
    """The account document, locked for the rest of the transaction."""
    return await session.get(User, user_id, for_update=True)


async def credential(session: Session, user_id: uuid.UUID) -> Credential | None:
    return await session.get(Credential, user_id)


@dataclass(frozen=True, slots=True)
class FailureState:
    count: int
    locked_until: datetime | None


@dataclass(frozen=True, slots=True)
class LockoutPolicy:
    threshold: int
    window: timedelta
    duration: timedelta


async def register_failure(
    session: Session, user_id: uuid.UUID, now: datetime, policy: LockoutPolicy
) -> FailureState:
    """Count a failed sign-in attempt in the current window; lock at the threshold. One atomic
    update, so concurrent failures are all counted."""
    window_expired = {
        "$or": [
            {"$eq": ["$failed_login_window_started_at", None]},
            {"$lte": ["$failed_login_window_started_at", now - policy.window]},
        ]
    }
    user = await session.find_one_and_update(
        User,
        {"id": user_id},
        [
            {"$set": {"_expired": window_expired}},
            {
                "$set": {
                    "failed_login_count": {"$cond": ["$_expired", 1, {"$add": ["$failed_login_count", 1]}]},
                    "failed_login_window_started_at": {
                        "$cond": ["$_expired", now, "$failed_login_window_started_at"]
                    },
                    "last_failed_login_at": now,
                }
            },
            {
                "$set": {
                    "locked_until": {
                        "$cond": [
                            {"$gte": ["$failed_login_count", policy.threshold]},
                            now + policy.duration,
                            "$locked_until",
                        ]
                    }
                }
            },
            {"$unset": "_expired"},
        ],
    )
    if user is None:
        raise LookupError("the account disappeared during sign-in")
    return FailureState(user.failed_login_count, user.locked_until)


async def clear_failures(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    await session.update_many(
        User,
        {"id": user_id},
        {
            "$set": {
                "failed_login_count": 0,
                "failed_login_window_started_at": None,
                "locked_until": None,
                "last_login_at": now,
                "updated_at": now,
            }
        },
    )


@dataclass(frozen=True, slots=True)
class UserPage:
    users: Sequence[User]
    next_cursor: uuid.UUID | None


async def list_users(
    session: Session,
    *,
    after: uuid.UUID | None,
    limit: int,
    email_contains: str | None = None,
    employee_id: uuid.UUID | None = None,
) -> UserPage:
    """Accounts in creation order (UUIDv7 IDs are time-ordered), keyset-paginated, optionally
    narrowed to an email fragment or the account linked to an employee record."""
    query: dict[str, Any] = {}
    if after is not None:
        query["id"] = {"$gt": after}
    if email_contains:
        query["email"] = {"$regex": _escape(email_contains), "$options": "i"}
    if employee_id is not None:
        query["employee_id"] = employee_id
    rows = await session.find(User, query, sort=[("id", 1)], limit=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return UserPage(rows, rows[-1].id if has_more and rows else None)


def _escape(text: str) -> str:
    return "".join(f"\\{c}" if c in r"\.^$|?*+()[]{}" else c for c in text)


# --- factors and recovery codes ---------------------------------------------------------


async def confirmed_factors(session: Session, user_id: uuid.UUID) -> Sequence[MfaFactor]:
    return await session.find(
        MfaFactor,
        {"user_id": user_id, "confirmed_at": {"$ne": None}, "revoked_at": None},
        sort=[("id", 1)],
    )


async def pending_factor(
    session: Session, user_id: uuid.UUID, factor_id: uuid.UUID, created_after: datetime
) -> MfaFactor | None:
    return await session.find_one(
        MfaFactor,
        {
            "id": factor_id,
            "user_id": user_id,
            "confirmed_at": None,
            "revoked_at": None,
            "created_at": {"$gt": created_after},
        },
    )


async def revoke_pending_factors(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    await session.update_many(
        MfaFactor,
        {"user_id": user_id, "confirmed_at": None, "revoked_at": None},
        {"$set": {"revoked_at": now, "updated_at": now}},
    )


async def record_factor_use(session: Session, factor_id: uuid.UUID, step: int, now: datetime) -> bool:
    """Accept a TOTP step only if it is later than the last one used (replay protection).
    Atomic, so two requests with the same code cannot both succeed."""
    used = await session.find_one_and_update(
        MfaFactor,
        {"id": factor_id, "$or": [{"last_used_step": None}, {"last_used_step": {"$lt": step}}]},
        {"$set": {"last_used_step": step, "last_used_at": now, "updated_at": now}},
    )
    return used is not None


async def use_recovery_code(session: Session, user_id: uuid.UUID, digest: bytes, now: datetime) -> bool:
    used = await session.find_one_and_update(
        RecoveryCode,
        {"user_id": user_id, "code_hash": digest, "used_at": None, "revoked_at": None},
        {"$set": {"used_at": now}},
    )
    return used is not None


async def revoke_all_factors(session: Session, user_id: uuid.UUID, now: datetime) -> int:
    """Revoke every factor of an account, confirmed or pending (an administrator's MFA reset).
    The caller sets `mfa_reenrolment_required` in the same transaction, or the MFA invariant
    refuses the commit."""
    return await session.update_many(
        MfaFactor, {"user_id": user_id, "revoked_at": None}, {"$set": {"revoked_at": now, "updated_at": now}}
    )


async def revoke_recovery_codes(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    await session.update_many(
        RecoveryCode, {"user_id": user_id, "used_at": None, "revoked_at": None}, {"$set": {"revoked_at": now}}
    )


async def replace_recovery_codes(
    session: Session, user_id: uuid.UUID, digests: Sequence[bytes], now: datetime
) -> None:
    await revoke_recovery_codes(session, user_id, now)
    session.add_all([RecoveryCode(user_id=user_id, code_hash=digest) for digest in digests])


async def unused_recovery_code_count(session: Session, user_id: uuid.UUID) -> int:
    return await session.count(RecoveryCode, {"user_id": user_id, "used_at": None, "revoked_at": None})


# --- one-time tokens --------------------------------------------------------------------


def _usable(digest: bytes, purpose: OneTimePurpose, now: datetime) -> dict[str, Any]:
    return {"token_hash": digest, "purpose": purpose.value, "used_at": None, "expires_at": {"$gt": now}}


async def lock_one_time_token(
    session: Session, digest: bytes, purpose: OneTimePurpose, now: datetime
) -> OneTimeToken | None:
    """A usable token (unused, unexpired), locked for the rest of the transaction; the unit of
    work then belongs to its organization."""
    found = _resolve(session, await session.find_any_tenant(OneTimeToken, _usable(digest, purpose, now)))
    if found is None:
        return None
    return await session.get(OneTimeToken, found.id, for_update=True)


async def find_one_time_token(
    session: Session, digest: bytes, purpose: OneTimePurpose, now: datetime
) -> OneTimeToken | None:
    return _resolve(session, await session.find_any_tenant(OneTimeToken, _usable(digest, purpose, now)))


async def retire_one_time_tokens(
    session: Session, user_id: uuid.UUID, purpose: OneTimePurpose, now: datetime
) -> None:
    """Mark every outstanding token of a purpose used, so only the newest one works."""
    await session.update_many(
        OneTimeToken,
        {"user_id": user_id, "purpose": purpose.value, "used_at": None},
        {"$set": {"used_at": now}},
    )


# --- sessions and session tokens --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessRecord:
    session: AuthSession
    user: User
    token_expires_at: datetime


async def access_record(session: Session, digest: bytes) -> AccessRecord | None:
    token = _resolve(
        session,
        await session.find_any_tenant(SessionToken, {"token_hash": digest, "kind": TokenKind.ACCESS.value}),
    )
    if token is None:
        return None
    record = await session.get(AuthSession, token.session_id)
    user = await session.get(User, record.user_id) if record else None
    if record is None or user is None:
        return None
    return AccessRecord(record, user, token.expires_at)


async def find_refresh_token(session: Session, digest: bytes) -> SessionToken | None:
    return _resolve(
        session,
        await session.find_any_tenant(SessionToken, {"token_hash": digest, "kind": TokenKind.REFRESH.value}),
    )


async def lock_refresh_token(session: Session, digest: bytes) -> SessionToken | None:
    found = await find_refresh_token(session, digest)
    if found is None:
        return None
    return await session.get(SessionToken, found.id, for_update=True)


async def expire_tokens(
    session: Session,
    *,
    now: datetime,
    session_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    retire_refresh: bool = True,
) -> None:
    """End the outstanding access tokens of one session or of every session of a user, and
    (unless `retire_refresh` is false) retire their unused refresh tokens."""
    sessions: dict[str, Any] = {}
    if session_id is not None:
        sessions["id"] = session_id
    if user_id is not None:
        sessions["user_id"] = user_id
    ids = await session.distinct(AuthSession, "id", sessions)
    if not ids:
        return
    await session.update_many(
        SessionToken,
        {"session_id": {"$in": ids}, "kind": TokenKind.ACCESS.value, "expires_at": {"$gt": now}},
        {"$set": {"expires_at": now}},
    )
    if retire_refresh:
        await session.update_many(
            SessionToken,
            {"session_id": {"$in": ids}, "kind": TokenKind.REFRESH.value, "used_at": None},
            {"$set": {"used_at": now}},
        )


async def active_sessions(session: Session, user_id: uuid.UUID, now: datetime) -> Sequence[AuthSession]:
    return await session.find(
        AuthSession,
        {
            "user_id": user_id,
            "revoked_at": None,
            "idle_expires_at": {"$gt": now},
            "absolute_expires_at": {"$gt": now},
        },
        sort=[("last_activity_at", -1)],
    )


async def revoke_sessions(
    session: Session,
    *,
    user_id: uuid.UUID,
    reason: RevokedReason,
    now: datetime,
    session_id: uuid.UUID | None = None,
    except_session_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Revoke one or all of a user's open sessions; returns the IDs revoked."""
    query: dict[str, Any] = {"user_id": user_id, "revoked_at": None}
    if session_id is not None:
        query["id"] = session_id
    if except_session_id is not None:
        query["id"] = {"$ne": except_session_id} if session_id is None else session_id
        if session_id is not None and session_id == except_session_id:
            return []
    revoked = await session.distinct(AuthSession, "id", query)
    if not revoked:
        return []
    await session.update_many(
        AuthSession,
        {"id": {"$in": revoked}, "revoked_at": None},
        {"$set": {"revoked_at": now, "revoked_reason": reason.value}},
    )
    for revoked_id in revoked:
        await expire_tokens(session, now=now, session_id=revoked_id)
    return list(revoked)


def session_expiry(now: datetime, absolute_expires_at: datetime, idle: timedelta) -> datetime:
    return min(now + idle, absolute_expires_at)


async def store_password(
    session: Session,
    user_id: uuid.UUID,
    password_hash: str,
    now: datetime,
    *,
    keep_changed_at: bool = False,
) -> None:
    credential = await session.get(Credential, user_id)
    if credential is None:
        session.add(Credential(user_id=user_id, password_hash=password_hash, password_changed_at=now))
        return
    credential.password_hash = password_hash
    credential.updated_at = now
    if not keep_changed_at:
        credential.password_changed_at = now


# --- trusted devices ---------------------------------------------------------------------


async def lock_trusted_device(
    session: Session, user_id: uuid.UUID, digest: bytes, now: datetime
) -> TrustedDevice | None:
    """The user's unrevoked, unexpired device with this cookie, locked."""
    found = await session.find_one(
        TrustedDevice,
        {"user_id": user_id, "token_hash": digest, "revoked_at": None, "expires_at": {"$gt": now}},
    )
    if found is None:
        return None
    return await session.get(TrustedDevice, found.id, for_update=True)


async def active_devices(session: Session, user_id: uuid.UUID, now: datetime) -> Sequence[TrustedDevice]:
    return await session.find(
        TrustedDevice,
        {"user_id": user_id, "revoked_at": None, "expires_at": {"$gt": now}},
        sort=[("last_seen_at", -1)],
    )


async def revoke_devices(
    session: Session,
    *,
    user_id: uuid.UUID,
    reason: str,
    now: datetime,
    device_id: uuid.UUID | None = None,
    except_digest: bytes | None = None,
) -> int:
    """Revoke one or all of a user's devices (optionally keeping the one with `except_digest`)."""
    query: dict[str, Any] = {"user_id": user_id, "revoked_at": None}
    if device_id is not None:
        query["id"] = device_id
    if except_digest is not None:
        query["token_hash"] = {"$ne": except_digest}
    return await session.update_many(
        TrustedDevice, query, {"$set": {"revoked_at": now, "revoked_reason": reason}}
    )


async def active_user_ids(session: Session, user_ids: Collection[uuid.UUID]) -> list[uuid.UUID]:
    """The given accounts that are active, in ID order."""
    if not user_ids:
        return []
    users = await session.find(
        User, {"id": {"$in": list(user_ids)}, "status": UserStatus.ACTIVE.value}, sort=[("id", 1)]
    )
    return [user.id for user in users]
