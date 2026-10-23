"""The identity module's part of permanently erasing one person's data (owner decision
2026-10-23, docs/database-design.md §9.1).

The account linked to the employee record is disabled (if it was not already, which ends every
session, trusted device and outstanding invite), then everything it could sign in with or that
describes how it was used is deleted: password, authenticators, recovery codes, sessions and
their tokens, single-use links and trusted browsers. The account document stays, because role
history, access requests and the audit log refer to it by ID: its email becomes a placeholder
that can never receive mail, and it is marked erased, so it can never be enabled again.

Sign-in history lives in the security events, which only the audit retention job removes (rule
19); it stays until then and is reported as retained.
"""

import uuid
from datetime import datetime

from app.modules.identity import accounts
from app.modules.identity import repository as repo
from app.modules.identity.models import (
    AuthSession,
    Credential,
    MfaFactor,
    OneTimeToken,
    RecoveryCode,
    SessionToken,
    TrustedDevice,
    UserStatus,
)
from app.platform.db import Session
from app.platform.erasure import Erased, Outcome


def erased_email(user_id: uuid.UUID) -> str:
    """A unique address under a reserved name (RFC 6761 `.invalid`) that no mail can reach."""
    return f"erased-{user_id.hex}@erased.invalid"


async def erase_account(
    session: Session, employee_id: uuid.UUID, now: datetime
) -> tuple[uuid.UUID | None, Erased]:
    """Erase the account linked to the employee, if any, in the caller's transaction; returns
    its ID and what was done."""
    result = Erased()
    linked = await repo.user_for_employee(session, employee_id)
    user = await repo.lock_user(session, linked.id) if linked is not None else None
    if user is None:
        result.add("identity.account", Outcome.RETAINED, 0, "No account was linked to this employee record.")
        return None, result
    if user.status != UserStatus.DISABLED:
        await accounts.disable(session, user, now)
    sessions = [found.id for found in await session.find(AuthSession, {"user_id": user.id})]
    tokens = await session.delete_many(SessionToken, {"session_id": {"$in": sessions}}) if sessions else 0
    removed_sessions = await session.delete_many(AuthSession, {"user_id": user.id})
    credential = await session.get(Credential, user.id)
    if credential is not None:
        await session.delete(credential)
    factors = await session.delete_many(MfaFactor, {"user_id": user.id})
    codes = await session.delete_many(RecoveryCode, {"user_id": user.id})
    links = await session.delete_many(OneTimeToken, {"user_id": user.id})
    devices = await session.delete_many(TrustedDevice, {"user_id": user.id})

    user.status = UserStatus.DISABLED.value
    user.email = erased_email(user.id)
    user.last_login_at = None
    user.failed_login_count = 0
    user.failed_login_window_started_at = None
    user.last_failed_login_at = None
    user.locked_until = None
    user.mfa_reenrolment_required = False
    user.erased_at = now
    user.updated_at = now
    user.version += 1
    await session.flush()

    result.add(
        "identity.account",
        Outcome.ANONYMIZED,
        1,
        "Disabled for good; the email is replaced by an address that cannot receive mail. The account ID "
        "stays because role history and the audit log refer to it.",
    )
    result.add(
        "identity.sign_in_credentials",
        Outcome.DELETED,
        int(credential is not None) + factors + codes,
        "Password, authenticator apps and recovery codes: nothing to sign in with remains.",
    )
    result.add(
        "identity.sessions",
        Outcome.DELETED,
        removed_sessions + tokens,
        "Sessions with their addresses and browsers, and their access and refresh tokens.",
    )
    result.add(
        "identity.single_use_links", Outcome.DELETED, links, "Invite, password reset and re-enrolment links."
    )
    result.add(
        "identity.trusted_browsers", Outcome.DELETED, devices, "Recognized browsers and their descriptions."
    )
    return user.id, result
