"""Emails about grant requests (docs/security-architecture.md §9).

All super admins are told when a request is made, decided, ends early or by time, or expires;
a break-glass use also reaches every auditor at once. The person who acted is not emailed
about their own action. Rows are written in the caller's transaction (notify outbox), name
only the recipient account, and carry display values only: never the free-text reason, which
could hold anything, and never an address.
"""

import uuid
from datetime import datetime

from app.modules.access import repository as repo
from app.modules.access.models import RoleGrantRequest
from app.modules.identity import public as identity
from app.modules.notify.public import EmailTemplate, enqueue
from app.platform.authz.roles import RoleKey
from app.platform.db import Session


async def grant_event(
    session: Session,
    request: RoleGrantRequest,
    event: str,
    now: datetime,
    *,
    exclude: uuid.UUID | None,
    include_auditors: bool = False,
) -> None:
    """`event`: requested, approved, rejected, revoked, ended, expired or break_glass."""
    roles = [RoleKey.SUPER_ADMIN.value, *([RoleKey.AUDITOR.value] if include_auditors else [])]
    await session.flush()
    holders = await repo.holder_ids(session, roles, now)
    minutes = (
        int(request.requested_duration.total_seconds() // 60)
        if request.requested_duration is not None
        else None
    )
    for user_id in await identity.active_user_ids(session, holders):
        if user_id == exclude:
            continue
        await enqueue(
            session,
            user_id=user_id,
            template=EmailTemplate.GRANT_REQUEST,
            idempotency_key=f"grant_request:{request.id}:{event}:{user_id}",
            now=now,
            data={"event": event, "kind": request.kind, "role": request.role_key, "minutes": minutes},
        )
