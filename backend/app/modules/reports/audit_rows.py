"""Audit log rows for the audit export (`audit.export`, product-requirements §4 row 24).

The audit log holds no sensitive values by design (docs/security-architecture.md §8): field
changes carry old and new values only for non-sensitive fields. The export carries the rows as
the audit log viewer shows them.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from app.platform.audit.tables import AuditLogRecord
from app.platform.csvfile import Cell
from app.platform.db import Session

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


async def audit_rows(session: Session, start: date, end: date) -> list[list[Cell]]:
    since = datetime.combine(start, time(), UTC)
    until = datetime.combine(end + timedelta(days=1), time(), UTC)
    records = await session.find(
        AuditLogRecord,
        {"recorded_at": {"$gte": since, "$lt": until}},
        sort=[("recorded_at", 1), ("id", 1)],
        limit=MAX_ROWS,
    )
    return [
        [
            r.recorded_at,
            r.occurred_at,
            r.action,
            r.outcome,
            r.actor_type,
            r.actor_user_id,
            r.grant_request_id,
            r.permission_used,
            r.target_type,
            r.target_id,
            r.subject_employee_id,
            r.request_id,
            r.ip,
            r.reason,
            json.dumps(r.changes, sort_keys=True) if r.changes is not None else None,
        ]
        for r in records
    ]
