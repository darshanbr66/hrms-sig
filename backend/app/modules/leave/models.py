"""SQLAlchemy models for the `leave` schema (docs/database-design.md §4.6): holiday calendars
(revision 0016; a trigger keeps holiday dates and location assignments inside the calendar's
year) and leave management (revision 0018)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Numeric, SmallInteger, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "leave"
# `date` is also a column name; the alias keeps the type reachable inside such a class.
Day = date


class HolidayCalendar(Base):
    __tablename__ = "holiday_calendars"
    __table_args__ = (UniqueConstraint("name", "year"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    name: Mapped[str]
    year: Mapped[int] = mapped_column(SmallInteger())
    status: Mapped[str] = mapped_column(server_default="active")
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HolidayKind(StrEnum):
    HOLIDAY = "holiday"
    # Worked although the shift has the date as a weekly off.
    WORKING_DAY = "working_day"


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("calendar_id", "date"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    calendar_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave.holiday_calendars.id", ondelete="RESTRICT")
    )
    date: Mapped[date]
    name: Mapped[str]
    is_optional: Mapped[bool] = mapped_column(server_default=text("false"))
    # `holiday`, or `working_day`: worked although the shift has it as a weekly off (revision 0027).
    kind: Mapped[str] = mapped_column(server_default=text("'holiday'"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LocationHolidayCalendar(Base):
    __tablename__ = "location_holiday_calendars"
    __table_args__ = (
        UniqueConstraint("location_id", "year"),
        Index("ix_location_holiday_calendars_calendar_id", "calendar_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("org.locations.id", ondelete="RESTRICT"))
    calendar_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave.holiday_calendars.id", ondelete="RESTRICT")
    )
    year: Mapped[int] = mapped_column(SmallInteger())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# --- leave management (revision 0018) -----------------------------------------------------------
# CHECK and exclusion constraints and the guard triggers are in the migration and covered by the
# schema tests; the enums mirror their allowed values.


class LeaveTypeStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AccrualMethod(StrEnum):
    NONE = "none"
    UPFRONT_PER_LEAVE_YEAR = "upfront_per_leave_year"
    PERIODIC = "periodic"


class AccrualFrequency(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class AccrualTiming(StrEnum):
    PERIOD_START = "period_start"
    PERIOD_END = "period_end"


class ProrationRounding(StrEnum):
    EXACT = "exact"
    NEAREST_HALF = "nearest_half"
    DOWN_HALF = "down_half"
    UP_HALF = "up_half"


class CarryForwardMode(StrEnum):
    NONE = "none"
    CAPPED = "capped"
    UNLIMITED = "unlimited"


class ExcessAction(StrEnum):
    LAPSE = "lapse"
    KEEP = "keep"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    CANCELLATION_REQUESTED = "cancellation_requested"


# The statuses that hold the dates: they cannot overlap and count against the balance.
ACTIVE_STATUSES = (RequestStatus.PENDING, RequestStatus.APPROVED, RequestStatus.CANCELLATION_REQUESTED)


class Half(StrEnum):
    FIRST = "first"
    SECOND = "second"


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLATION_APPROVED = "cancellation_approved"
    CANCELLATION_REJECTED = "cancellation_rejected"


class EntryType(StrEnum):
    OPENING_BALANCE = "opening_balance"
    ACCRUAL = "accrual"
    USAGE = "usage"
    USAGE_REVERSAL = "usage_reversal"
    ADJUSTMENT = "adjustment"
    CARRY_FORWARD_OUT = "carry_forward_out"
    CARRY_FORWARD = "carry_forward"
    CARRY_FORWARD_EXPIRY = "carry_forward_expiry"
    LAPSE = "lapse"


class LeaveType(Base):
    __tablename__ = "leave_types"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    code: Mapped[str] = mapped_column(CITEXT(), unique=True)
    name: Mapped[str]
    description: Mapped[str | None]
    is_paid: Mapped[bool]
    requires_approval: Mapped[bool]
    allows_half_day: Mapped[bool]
    counts_weekly_offs: Mapped[bool]
    counts_holidays: Mapped[bool]
    status: Mapped[str] = mapped_column(server_default=LeaveTypeStatus.ACTIVE.value)
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LeavePolicy(Base):
    __tablename__ = "leave_policies"
    __table_args__ = (Index("ix_leave_policies_leave_type_id", "leave_type_id"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    leave_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leave.leave_types.id", ondelete="RESTRICT"))
    location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org.locations.id", ondelete="RESTRICT"))
    employment_type: Mapped[str | None]
    effective_from: Mapped[date]
    # Exclusive; null while open-ended.
    effective_to: Mapped[date | None]
    leave_year_start_month: Mapped[int] = mapped_column(SmallInteger())
    leave_year_start_day: Mapped[int] = mapped_column(SmallInteger())
    accrual_method: Mapped[str]
    accrual_amount: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    allow_negative_balance: Mapped[bool]
    max_negative_days: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # Revision 0021. Null where the rule does not apply or was not decided (policies written
    # before 0021); the service requires a decision on new policies where it applies.
    accrual_frequency: Mapped[str | None]
    accrual_timing: Mapped[str | None]
    prorate_on_joining: Mapped[bool | None]
    prorate_on_exit: Mapped[bool | None]
    proration_rounding: Mapped[str | None]
    # Null: the year-end rule is not decided, and the year-end run skips the policy.
    carry_forward_mode: Mapped[str | None]
    carry_forward_cap: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    excess_action: Mapped[str | None]
    carried_expire_after_months: Mapped[int | None] = mapped_column(SmallInteger())
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        UniqueConstraint("employee_id", "idempotency_key"),
        Index("ix_leave_requests_status_start_date", "status", "start_date"),
        Index("ix_leave_requests_leave_type_id", "leave_type_id"),
        Index("ix_leave_requests_employee_id_start_date", "employee_id", "start_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.employees.id", ondelete="RESTRICT"))
    leave_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leave.leave_types.id", ondelete="RESTRICT"))
    start_date: Mapped[date]
    end_date: Mapped[date]
    start_half: Mapped[str | None]
    end_half: Mapped[str | None]
    days_requested: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    # The `personal` tier: shown to the employee and to whoever decides the request.
    reason: Mapped[str | None]
    status: Mapped[str]
    submitted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str]
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LeaveRequestDay(Base):
    __tablename__ = "leave_request_days"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    leave_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave.leave_requests.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[Day] = mapped_column(primary_key=True)
    portion: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    counted: Mapped[bool]
    not_counted_reason: Mapped[str | None]


class LeaveDecision(Base):
    __tablename__ = "leave_decisions"
    __table_args__ = (
        UniqueConstraint("decided_by", "idempotency_key"),
        Index("ix_leave_decisions_leave_request_id", "leave_request_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    leave_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave.leave_requests.id", ondelete="RESTRICT")
    )
    step: Mapped[int] = mapped_column(SmallInteger(), server_default=text("1"))
    decision: Mapped[str]
    decided_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    note: Mapped[str | None]
    idempotency_key: Mapped[str]
    decided_at: Mapped[datetime] = mapped_column(server_default=func.now())


# The predicates of the ledger's unique indexes (revision 0021): one opening balance, upfront
# allocation and year-end entry of each kind per leave year; one periodic accrual per period.
YEARLY_WHERE = (
    "entry_type IN ('opening_balance', 'carry_forward_out', 'carry_forward', 'carry_forward_expiry', "
    "'lapse') OR (entry_type = 'accrual' AND period_start IS NULL)"
)
PERIODIC_WHERE = "entry_type = 'accrual' AND period_start IS NOT NULL"


class LeaveLedgerEntry(Base):
    """Append-only (trigger `leave.guard_append_only`). The balance is the sum of the entries."""

    __tablename__ = "leave_ledger"
    __table_args__ = (
        Index(
            "ix_leave_ledger_employee_id_leave_type_id_effective_date",
            "employee_id",
            "leave_type_id",
            "effective_date",
        ),
        Index(
            "uq_leave_ledger_yearly",
            "employee_id",
            "leave_type_id",
            "leave_year",
            "entry_type",
            unique=True,
            postgresql_where=text(YEARLY_WHERE),
        ),
        Index(
            "uq_leave_ledger_periodic",
            "employee_id",
            "leave_type_id",
            "period_start",
            unique=True,
            postgresql_where=text(PERIODIC_WHERE),
        ),
        Index(
            "ix_leave_ledger_import_batch_id",
            "import_batch_id",
            postgresql_where=text("import_batch_id IS NOT NULL"),
        ),
        Index(
            "uq_leave_ledger_request",
            "leave_request_id",
            "entry_type",
            unique=True,
            postgresql_where=text("leave_request_id IS NOT NULL"),
        ),
        Index(
            "uq_leave_ledger_employee_id_idempotency_key",
            "employee_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.employees.id", ondelete="RESTRICT"))
    leave_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leave.leave_types.id", ondelete="RESTRICT"))
    # The first day of the leave year the entry belongs to.
    leave_year: Mapped[date]
    entry_type: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    effective_date: Mapped[date]
    leave_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leave.leave_requests.id", ondelete="RESTRICT")
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leave.leave_policies.id", ondelete="RESTRICT")
    )
    reason: Mapped[str | None]
    # Null only for entries the worker writes as the system (periodic accrual, expiry).
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str | None]
    # The accrual period a periodic accrual belongs to.
    period_start: Mapped[date | None]
    # Opening balances imported together share a batch.
    import_batch_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
