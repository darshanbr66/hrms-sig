"""SQLAlchemy models for the `attendance` schema (docs/database-design.md §4.5, revision 0015).

Events are append-only (database trigger `attendance.guard_events`). Allowed values are CHECK
constraints in the migration; `timeline.EventType` mirrors them.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "attendance"


class EventSource(StrEnum):
    WEB = "web"
    MOBILE = "mobile"
    CORRECTION = "correction"
    IMPORT = "import"


class AttendanceEvent(Base):
    __tablename__ = "attendance_events"
    __table_args__ = (
        UniqueConstraint("employee_id", "idempotency_key"),
        Index(
            "ix_attendance_events_employee_id_work_date_occurred_at",
            "employee_id",
            "work_date",
            "occurred_at",
        ),
        Index("ix_attendance_events_work_date", "work_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.employees.id", ondelete="RESTRICT"))
    event_type: Mapped[str]
    # Server time for live events (product-requirements ATT-2).
    occurred_at: Mapped[datetime]
    work_date: Mapped[date]
    source: Mapped[str]
    recorded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Metadata only; never used as the event time.
    client_reported_at: Mapped[datetime | None]
    ip: Mapped[str | None] = mapped_column(INET())
    user_agent: Mapped[str | None]
    idempotency_key: Mapped[str]
