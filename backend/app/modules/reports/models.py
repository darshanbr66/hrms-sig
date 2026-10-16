"""SQLAlchemy model for `app.export_jobs` (docs/database-design.md §4.11, revision 0023)."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, SmallInteger, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base


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


class ExportJob(Base):
    __tablename__ = "export_jobs"
    __table_args__ = (
        Index(
            "ix_export_jobs_due",
            "requested_at",
            postgresql_where=text("status IN ('queued', 'running', 'done')"),
        ),
        Index("ix_export_jobs_requested_by_requested_at", "requested_by", "requested_at"),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    export_type: Mapped[str]
    # The period and filters, and the employees the requester could read when asking.
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB())
    status: Mapped[str] = mapped_column(server_default=ExportStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(SmallInteger(), server_default=text("0"))
    row_count: Mapped[int | None]
    storage_key: Mapped[str | None] = mapped_column(unique=True)
    file_name: Mapped[str | None]
    error_code: Mapped[str | None]
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    downloaded_at: Mapped[datetime | None]
