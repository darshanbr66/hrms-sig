"""A user's own sign-in history, read from `security_events` (AUTH-7).

Only the sign-in subset, only the user's own rows, and only the columns the account page
shows: never the event details.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.platform.audit.records import SecurityEventType
from app.platform.audit.tables import SecurityEventRecord
from app.platform.db import Session

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
    session: Session,
    user_id: uuid.UUID,
    *,
    since: datetime,
    before: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[LoginHistoryRow]:
    # Security events are not tenant-scoped (some have no organization), so the organization is
    # named here, besides the account.
    query: dict[str, Any] = {
        "organization_id": session.require_tenant(),
        "user_id": user_id,
        "event_type": {"$in": list(LOGIN_EVENT_TYPES)},
        "recorded_at": {"$gte": since},
    }
    if before is not None:
        query = {
            "$and": [
                query,
                {
                    "$or": [
                        {"recorded_at": {"$lt": before[0]}},
                        {"recorded_at": before[0], "id": {"$lt": before[1]}},
                    ]
                },
            ]
        }
    rows = await session.find(SecurityEventRecord, query, sort=[("recorded_at", -1), ("id", -1)], limit=limit)
    return [
        LoginHistoryRow(row.id, row.recorded_at, row.occurred_at, row.event_type, row.ip, row.user_agent)
        for row in rows
    ]
