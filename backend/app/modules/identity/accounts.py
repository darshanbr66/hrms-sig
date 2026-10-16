"""Disabling an account: the steps shared by an administrator's disable and an employee's exit.

The account stops working at once: its status, every session (access and refresh tokens),
every trusted device, any outstanding invite token and queued invite email. The caller has
locked the account row and writes the audit records.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import devices, notifications
from app.modules.identity import repository as repo
from app.modules.identity.models import DeviceRevokedReason, OneTimePurpose, RevokedReason, User, UserStatus


async def disable(session: AsyncSession, user: User, now: datetime) -> list[uuid.UUID]:
    """Disable a locked account; returns the sessions revoked."""
    user.status = UserStatus.DISABLED.value
    revoked = await repo.revoke_sessions(
        session, user_id=user.id, reason=RevokedReason.ACCOUNT_DISABLED, now=now
    )
    await devices.revoke_all(session, user_id=user.id, reason=DeviceRevokedReason.ACCOUNT_DISABLED, now=now)
    # An outstanding invite stops working too.
    for purpose in (OneTimePurpose.INVITE, OneTimePurpose.INVITE_ENROLMENT):
        await repo.retire_one_time_tokens(session, user.id, purpose, now)
    await notifications.cancel_invites(session, user.id, now)
    user.updated_at = now
    user.version += 1
    return revoked
