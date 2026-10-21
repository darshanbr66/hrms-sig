"""Export jobs (docs/database-design.md §4.11): one document per export someone asked for."""

import dataclasses
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import Index, Record, allowed, asc, utcnow


class ExportType(StrEnum):
    ATTENDANCE_SUMMARY = "attendance_summary"
    ATTENDANCE_DAYS = "attendance_days"
    LEAVE_REQUESTS = "leave_requests"
    LEAVE_BALANCES = "leave_balances"
    EMPLOYEES = "employees"
    AUDIT_LOG = "audit_log"


class ExportStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclasses.dataclass(kw_only=True, eq=False)
class ExportJob(Record):
    __collection__ = "export_jobs"
    __deletable__ = False
    __indexes__ = (
        Index(asc("organization_id", "status", "requested_at", "_id"), name="ix_export_jobs_due"),
        Index(asc("organization_id", "requested_by", "requested_at"), name="ix_export_jobs_requested_by"),
        Index(
            asc("storage_key"),
            name="uq_export_jobs_storage_key",
            unique=True,
            partial={"storage_key": {"$type": "string"}},
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "export_type": allowed(ExportType),
        "status": allowed(ExportStatus),
        "storage_key": {"pattern": "^exports/[0-9a-f-]{36}$"},
        "file_name": {"minLength": 1, "maxLength": 120},
        "error_code": {"minLength": 1, "maxLength": 100},
        "attempts": {"minimum": 0, "maximum": 100},
    }
    __expr_rules__ = (
        {
            "$or": [
                {"$ne": ["$status", "done"]},
                {
                    "$and": [
                        {"$ne": ["$storage_key", None]},
                        {"$ne": ["$completed_at", None]},
                        {"$ne": ["$expires_at", None]},
                    ]
                },
            ]
        },
    )

    requested_by: uuid.UUID
    export_type: str
    # The period and filters, and the employees the requester could read when asking.
    parameters: dict[str, Any]
    status: str = ExportStatus.QUEUED.value
    attempts: int = 0
    row_count: int | None = None
    storage_key: str | None = None
    file_name: str | None = None
    error_code: str | None = None
    requested_at: datetime = dataclasses.field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    downloaded_at: datetime | None = None
