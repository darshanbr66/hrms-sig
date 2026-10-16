"""Audit log rows for the audit export (`audit.export`, product-requirements §4 row 24).

The audit log holds no sensitive values by design (docs/security-architecture.md §8): field
changes carry old and new values only for non-sensitive fields. The export carries the rows as
the audit log viewer shows them.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit.tables import audit_log
from app.platform.csvfile import Cell

# More than a quarter's activity at the design load; a longer export is a sign of the wrong period.
MAX_ROWS: Final = 200_000
AUDIT_HEADER: Final = (
    "recorded_at",
    "occurred_at",
    "action",
    "outcome",
    "actor_type",
    "actor_user_id",
    "grant_request_id",
    "permission_used",
    "target_type",
    "target_id",
    "subject_employee_id",
    "request_id",
    "ip",
    "reason",
    "changes",
)


async def audit_rows(session: AsyncSession, start: date, end: date) -> list[list[Cell]]:
    since = datetime.combine(start, time(), UTC)
    until = datetime.combine(end + timedelta(days=1), time(), UTC)
    c = audit_log.c
    result = await session.execute(
        select(
            c.recorded_at,
            c.occurred_at,
            c.action,
            c.outcome,
            c.actor_type,
            c.actor_user_id,
            c.grant_request_id,
            c.permission_used,
            c.target_type,
            c.target_id,
            c.subject_employee_id,
            c.request_id,
            c.ip,
            c.reason,
            c.changes,
        )
        .where(c.recorded_at >= since, c.recorded_at < until)
        .order_by(c.recorded_at, c.id)
        .limit(MAX_ROWS)
    )
    return [
        [
            *row[:12],
            str(row[12]) if row[12] is not None else None,
            row[13],
            json.dumps(row[14], sort_keys=True) if row[14] is not None else None,
        ]
        for row in result.all()
    ]
