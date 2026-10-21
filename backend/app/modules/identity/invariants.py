"""The MFA rules that span collections (CLAUDE.md rule 15, docs/security-architecture.md §3).

1. An active account that is not waiting to re-enrol after an administrator's MFA reset has at
   least one confirmed, unrevoked factor: the last factor can never be removed, and an account
   cannot become active without one.
2. An open full session belongs to an account with a confirmed factor that is not waiting to
   re-enrol: sessions of a reset account are enrolment-only.

MongoDB cannot express these, so every change that could break them registers `require_mfa`,
which checks both before the unit of work commits (after its last write, inside the same
transaction); a violation aborts the transaction, so nothing of the change is stored. The
check locks the account document first: two transactions that each remove a different factor
(each still seeing the other's) then conflict on the account, and the one retried sees the
other's removal, so no write skew can leave an account without MFA. The services prevent
these states first; this is the backstop, as the database triggers were.
"""

import uuid

from app.modules.identity.models import AuthSession, MfaFactor, User, UserStatus
from app.platform.db import Session


class MfaInvariantError(RuntimeError):
    """A change would leave an account active, or a full session open, without MFA."""


def require_mfa(session: Session, user_id: uuid.UUID) -> None:
    """Check the account's MFA rules before this unit of work commits."""

    async def check(current: Session) -> None:
        await _check(current, user_id)

    session.check_before_commit(f"identity.mfa:{user_id}", check)


async def _check(session: Session, user_id: uuid.UUID) -> None:
    user = await session.get(User, user_id, for_update=True)
    if user is None:
        return
    has_factor = await session.exists(
        MfaFactor, {"user_id": user_id, "confirmed_at": {"$ne": None}, "revoked_at": None}
    )
    if user.status == UserStatus.ACTIVE and not user.mfa_reenrolment_required and not has_factor:
        raise MfaInvariantError("an active account needs a confirmed MFA factor")
    if user.mfa_reenrolment_required or not has_factor:
        open_full = await session.exists(
            AuthSession, {"user_id": user_id, "scope": "full", "revoked_at": None}
        )
        if open_full:
            raise MfaInvariantError("a full session needs a confirmed MFA factor")
