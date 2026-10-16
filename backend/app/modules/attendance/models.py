"""SQLAlchemy models for the `attendance` schema (docs/database-design.md §4.5): events
(revision 0015); shifts, policies, corrections, voids and locks (revision 0020).

Events, voids and correction items are append-only, versioned rows change only to close an
open range, and a correction is decided once (database triggers). Allowed values are CHECK
constraints in the migrations; the enums here and `timeline.EventType` mirror them.
"""

import uuid
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
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
    # Server time for live events (product-requirements ATT-2); the approved time for
    # correction events.
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
    # Set on events an approved correction added (source `correction`).
    correction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attendance.attendance_corrections.id", ondelete="RESTRICT")
    )


class ShiftStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    code: Mapped[str] = mapped_column(CITEXT(), unique=True)
    name: Mapped[str]
    description: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=ShiftStatus.ACTIVE.value)
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ShiftVersion(Base):
    __tablename__ = "shift_versions"
    __table_args__ = (UniqueConstraint("shift_id", "version_no"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    shift_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attendance.shifts.id", ondelete="RESTRICT"))
    version_no: Mapped[int]
    effective_from: Mapped[date]
    # Exclusive; null while open-ended.
    effective_to: Mapped[date | None]
    start_time: Mapped[time]
    end_time: Mapped[time]
    crosses_midnight: Mapped[bool]
    # [{"weekday": 1-7 (Monday is 1), "weeks": null or [1-5]}]: see `derive.WeeklyOff`.
    weekly_offs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB())
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LocationDefaultShift(Base):
    __tablename__ = "location_default_shifts"
    __table_args__ = (Index("ix_location_default_shifts_shift_id", "shift_id"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("org.locations.id", ondelete="RESTRICT"))
    shift_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attendance.shifts.id", ondelete="RESTRICT"))
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ShiftAssignment(Base):
    __tablename__ = "shift_assignments"
    __table_args__ = (Index("ix_shift_assignments_shift_id", "shift_id"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.employees.id", ondelete="RESTRICT"))
    shift_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attendance.shifts.id", ondelete="RESTRICT"))
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PolicyScope(StrEnum):
    ORGANIZATION = "organization"
    LOCATION = "location"
    SHIFT = "shift"


class AttendancePolicy(Base):
    """How a day is judged (product-requirements §6.12). Every value is Sigvitas's."""

    __tablename__ = "attendance_policies"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    scope_type: Mapped[str]
    scope_id: Mapped[uuid.UUID | None]
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]
    grace_minutes: Mapped[int]
    early_departure_grace_minutes: Mapped[int]
    duration_basis: Mapped[str]
    full_day_min_minutes: Mapped[int]
    half_day_min_minutes: Mapped[int]
    max_break_minutes: Mapped[int | None]
    breaks_count_as_work: Mapped[bool]
    break_excess_action: Mapped[str]
    overtime_enabled: Mapped[bool]
    overtime_basis: Mapped[str | None]
    overtime_min_minutes: Mapped[int | None]
    overtime_requires_approval: Mapped[bool]
    incomplete_after_minutes: Mapped[int]
    allow_multiple_sessions: Mapped[bool]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CorrectionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CorrectionKind(StrEnum):
    # The employee asks; a manager or HR decides.
    REQUEST = "request"
    # HR records the change on the employee's behalf; it applies at once (SOD-7).
    HR_RECORDED = "hr_recorded"


class AttendanceCorrection(Base):
    __tablename__ = "attendance_corrections"
    __table_args__ = (
        UniqueConstraint("employee_id", "idempotency_key"),
        Index(
            "uq_attendance_corrections_pending",
            "employee_id",
            "work_date",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_attendance_corrections_status_work_date", "status", "work_date"),
        Index("ix_attendance_corrections_employee_id_work_date", "employee_id", "work_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.employees.id", ondelete="RESTRICT"))
    work_date: Mapped[date]
    kind: Mapped[str]
    # The `personal` tier: shown to the employee and to whoever may decide the correction.
    reason: Mapped[str]
    status: Mapped[str]
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None]
    # The work date was inside an active attendance lock when the correction was recorded.
    locked_period: Mapped[bool] = mapped_column(server_default=text("false"))
    idempotency_key: Mapped[str]
    version: Mapped[int] = mapped_column(server_default=text("1"))


class CorrectionAction(StrEnum):
    ADD = "add"
    VOID = "void"


class AttendanceCorrectionItem(Base):
    __tablename__ = "attendance_correction_items"
    __table_args__ = (
        Index("ix_attendance_correction_items_correction_id", "correction_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    correction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attendance.attendance_corrections.id", ondelete="RESTRICT")
    )
    action: Mapped[str]
    event_type: Mapped[str | None]
    proposed_at: Mapped[datetime | None]
    target_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attendance.attendance_events.id", ondelete="RESTRICT")
    )


class AttendanceEventVoid(Base):
    """An event an approved correction voided. The event itself stays."""

    __tablename__ = "attendance_event_voids"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attendance.attendance_events.id", ondelete="RESTRICT"), unique=True
    )
    correction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attendance.attendance_corrections.id", ondelete="RESTRICT")
    )
    voided_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    voided_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AttendanceLock(Base):
    __tablename__ = "attendance_locks"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    period_start: Mapped[date]
    # Inclusive.
    period_end: Mapped[date]
    reason: Mapped[str]
    locked_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    locked_at: Mapped[datetime] = mapped_column(server_default=func.now())
    released_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("identity.users.id", ondelete="RESTRICT")
    )
    released_at: Mapped[datetime | None]
    release_reason: Mapped[str | None]
