"""A user's own sign-in history, read from `audit.security_events` (AUTH-7).

Only the sign-in subset, only the user's own rows, and only the columns the account page
shows: never the event details.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit.records import SecurityEventType
from app.platform.audit.tables import security_events

LOGIN_EVENT_TYPES: Final = (
    SecurityEventType.LOGIN_SUCCEEDED.value,
    SecurityEventType.LOGIN_FAILED.value,
    SecurityEventType.ACCOUNT_LOCKED.value,
)


@dataclass(frozen=True, slots=True)
class LoginHistoryRow:
    id: uuid.UUID
    recorded_at: datetime
    occurred_at: datetime
    event_type: str
    ip: str | None
    user_agent: str | None


async def login_events(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    since: datetime,
    before: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[LoginHistoryRow]:
    table = security_events
    query = (
        select(
            table.c.id,
            table.c.recorded_at,
            table.c.occurred_at,
            table.c.event_type,
            table.c.ip,
            table.c.user_agent,
        )
        .where(
            table.c.user_id == user_id,
            table.c.event_type.in_(LOGIN_EVENT_TYPES),
            table.c.recorded_at >= since,
        )
        .order_by(table.c.recorded_at.desc(), table.c.id.desc())
        .limit(limit)
    )
    if before is not None:
        query = query.where(tuple_(table.c.recorded_at, table.c.id) < tuple_(*before))
    return [
        LoginHistoryRow(
            row.id,
            row.recorded_at,
            row.occurred_at,
            row.event_type,
            str(row.ip) if row.ip is not None else None,
            row.user_agent,
        )
        for row in (await session.execute(query)).all()
    ]
