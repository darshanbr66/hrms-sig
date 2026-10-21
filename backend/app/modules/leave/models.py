"""Holiday calendars and leave management (docs/database-design.md §4.6).

- Leave policies are versioned: only an open `effective_to` (and `updated_at`) may be set when a
  newer version takes over; they are never deleted. Policies of one scope never overlap, and
  active requests of one employee never overlap: both are checked by the service under a lock
  (formerly exclusion constraints).
- The ledger is append-only; the balance is the sum of its entries. Its unique indexes allow
  one opening balance, upfront allocation and year-end entry of each kind per leave year, one
  periodic accrual per period, one usage and one reversal per request, one adjustment per
  idempotency key. Which yearly or periodic slot an entry takes is the stored `slot` field
  (kept current on every write), since a partial index cannot test an `OR`.
- A holiday's date and a location's calendar year match the calendar's year (checked by the
  service; formerly triggers). Allowed values and the rules within one document are part of
  each collection's validator.
"""

import dataclasses
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Final

from app.platform.db import CASE_INSENSITIVE, Index, Record, allowed, asc, utcnow
from app.platform.ids import uuid7


@dataclasses.dataclass(kw_only=True, eq=False)
class HolidayCalendar(Record):
    __collection__ = "holiday_calendars"
    __indexes__ = (
        Index(
            asc("organization_id", "name", "year"),
            name="uq_holiday_calendars_name_year",
            unique=True,
            collation=CASE_INSENSITIVE,
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "name": {"minLength": 1, "maxLength": 200},
        "year": {"minimum": 2000, "maximum": 2100},
        "status": allowed(("active", "archived")),
        "version": {"minimum": 1},
    }

    name: str
    year: int
    status: str = "active"
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


class HolidayKind(StrEnum):
    HOLIDAY = "holiday"
    # Worked although the shift has the date as a weekly off.
    WORKING_DAY = "working_day"


@dataclasses.dataclass(kw_only=True, eq=False)
class Holiday(Record):
    __collection__ = "holidays"
    __indexes__ = (
        Index(asc("organization_id", "calendar_id", "date"), name="uq_holidays_calendar_date", unique=True),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "name": {"minLength": 1, "maxLength": 200},
        "kind": allowed(HolidayKind),
    }

    calendar_id: uuid.UUID
    date: date
    name: str
    is_optional: bool = False
    # `holiday`, or `working_day`: worked although the shift has it as a weekly off.
    kind: str = HolidayKind.HOLIDAY.value
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class LocationHolidayCalendar(Record):
    __collection__ = "location_holiday_calendars"
    __indexes__ = (
        Index(
            asc("organization_id", "location_id", "year"),
            name="uq_location_holiday_calendars_year",
            unique=True,
        ),
        Index(asc("organization_id", "calendar_id"), name="ix_location_holiday_calendars_calendar_id"),
    )

    location_id: uuid.UUID
    calendar_id: uuid.UUID
    year: int
    created_at: datetime = dataclasses.field(default_factory=utcnow)


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


@dataclasses.dataclass(kw_only=True, eq=False)
class LeaveType(Record):
    __collection__ = "leave_types"
    __deletable__ = False
    __indexes__ = (
        Index(
            asc("organization_id", "code"),
            name="uq_leave_types_code",
            unique=True,
            collation=CASE_INSENSITIVE,
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "code": {"minLength": 1, "maxLength": 32, "pattern": r"^\S+$"},
        "name": {"minLength": 1, "maxLength": 200},
        "description": {"minLength": 1, "maxLength": 1000},
        "status": allowed(LeaveTypeStatus),
        "version": {"minimum": 1},
    }

    code: str
    name: str
    description: str | None = None
    is_paid: bool
    requires_approval: bool
    allows_half_day: bool
    counts_weekly_offs: bool
    counts_holidays: bool
    status: str = LeaveTypeStatus.ACTIVE.value
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


def _null(field: str) -> dict[str, Any]:
    return {"$eq": [f"${field}", None]}


def _set(field: str) -> dict[str, Any]:
    return {"$ne": [f"${field}", None]}


def _is(field: str, *values: str) -> dict[str, Any]:
    return {"$in": [f"${field}", list(values)]}


@dataclasses.dataclass(kw_only=True, eq=False)
class LeavePolicy(Record):
    __collection__ = "leave_policies"
    __deletable__ = False
    __mutable__ = frozenset({"effective_to", "updated_at"})
    __set_once__ = frozenset({"effective_to"})
    __indexes__ = (
        Index(
            asc("organization_id", "leave_type_id", "location_id", "employment_type", "effective_from"),
            name="ix_leave_policies_scope",
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "employment_type": allowed(("full_time", "part_time", "contract", "intern")),
        "leave_year_start_month": {"minimum": 1, "maximum": 12},
        "leave_year_start_day": {"minimum": 1, "maximum": 31},
        "accrual_method": allowed(AccrualMethod),
        "accrual_frequency": allowed(AccrualFrequency),
        "accrual_timing": allowed(AccrualTiming),
        "proration_rounding": allowed(ProrationRounding),
        "carry_forward_mode": allowed(CarryForwardMode),
        "excess_action": allowed(ExcessAction),
        "carried_expire_after_months": {"minimum": 1, "maximum": 24},
    }
    __expr_rules__ = (
        {"$or": [_null("effective_to"), {"$gt": ["$effective_to", "$effective_from"]}]},
        {
            "$or": [
                {
                    "$and": [
                        _is("accrual_method", "none"),
                        _null("accrual_amount"),
                        _null("accrual_frequency"),
                        _null("accrual_timing"),
                        _null("prorate_on_joining"),
                        _null("prorate_on_exit"),
                    ]
                },
                {
                    "$and": [
                        _is("accrual_method", "upfront_per_leave_year"),
                        {"$gt": ["$accrual_amount", 0]},
                        _null("accrual_frequency"),
                        _null("accrual_timing"),
                        _null("prorate_on_exit"),
                    ]
                },
                {
                    "$and": [
                        _is("accrual_method", "periodic"),
                        {"$gt": ["$accrual_amount", 0]},
                        _set("accrual_frequency"),
                        _set("accrual_timing"),
                        _set("prorate_on_joining"),
                        _set("prorate_on_exit"),
                    ]
                },
            ]
        },
        {"$or": [_null("accrual_amount"), {"$lte": ["$accrual_amount", 366]}]},
        {
            "$or": [
                {"$and": ["$allow_negative_balance", {"$gte": ["$max_negative_days", 0]}]},
                {"$and": [{"$not": ["$allow_negative_balance"]}, _null("max_negative_days")]},
            ]
        },
        {
            "$eq": [
                {"$or": [{"$eq": ["$prorate_on_joining", True]}, {"$eq": ["$prorate_on_exit", True]}]},
                _set("proration_rounding"),
            ]
        },
        {
            "$or": [
                {
                    "$and": [
                        _null("carry_forward_mode"),
                        _null("carry_forward_cap"),
                        _null("excess_action"),
                        _null("carried_expire_after_months"),
                    ]
                },
                {
                    "$and": [
                        _is("carry_forward_mode", "none"),
                        _null("carry_forward_cap"),
                        _set("excess_action"),
                        _null("carried_expire_after_months"),
                    ]
                },
                {
                    "$and": [
                        _is("carry_forward_mode", "capped"),
                        {"$gte": ["$carry_forward_cap", 0]},
                        _set("excess_action"),
                    ]
                },
                {
                    "$and": [
                        _is("carry_forward_mode", "unlimited"),
                        _null("carry_forward_cap"),
                        _null("excess_action"),
                    ]
                },
            ]
        },
    )

    leave_type_id: uuid.UUID
    location_id: uuid.UUID | None = None
    employment_type: str | None = None
    effective_from: date
    # Exclusive; None while open-ended.
    effective_to: date | None = None
    leave_year_start_month: int
    leave_year_start_day: int
    accrual_method: str
    accrual_amount: Decimal | None = None
    allow_negative_balance: bool
    max_negative_days: Decimal | None = None
    # None where the rule does not apply or was not decided; the service requires a decision on
    # new policies where it applies.
    accrual_frequency: str | None = None
    accrual_timing: str | None = None
    prorate_on_joining: bool | None = None
    prorate_on_exit: bool | None = None
    proration_rounding: str | None = None
    # None: the year-end rule is not decided, and the year-end run skips the policy.
    carry_forward_mode: str | None = None
    carry_forward_cap: Decimal | None = None
    excess_action: str | None = None
    carried_expire_after_months: int | None = None
    created_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class LeaveRequestDay:
    """How one day of a request was counted; kept inside its request (`days`)."""

    date: date
    portion: Decimal
    counted: bool
    not_counted_reason: str | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class LeaveRequest(Record):
    __collection__ = "leave_requests"
    __deletable__ = False
    __indexes__ = (
        Index(
            asc("organization_id", "employee_id", "idempotency_key"),
            name="uq_leave_requests_idempotency_key",
            unique=True,
        ),
        Index(asc("organization_id", "status", "start_date"), name="ix_leave_requests_status_start_date"),
        Index(asc("organization_id", "leave_type_id"), name="ix_leave_requests_leave_type_id"),
        Index(asc("organization_id", "employee_id", "start_date"), name="ix_leave_requests_employee_start"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "start_half": allowed(Half),
        "end_half": allowed(Half),
        "reason": {"minLength": 1, "maxLength": 1000},
        "status": allowed(RequestStatus),
        "idempotency_key": {"minLength": 8, "maxLength": 100},
        "version": {"minimum": 1},
        "days": {"maxItems": 400},
    }
    __expr_rules__ = (
        {"$gte": ["$end_date", "$start_date"]},
        {"$gt": ["$days_requested", 0]},
    )

    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    start_half: str | None = None
    end_half: str | None = None
    days_requested: Decimal
    # The `personal` tier: shown to the employee and to whoever decides the request.
    reason: str | None = None
    status: str
    submitted_by: uuid.UUID
    idempotency_key: str
    version: int = 1
    # How each day was counted (formerly `leave_request_days`), written with the request.
    days: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)

    def day_rows(self) -> list[LeaveRequestDay]:
        return [
            LeaveRequestDay(
                date=date.fromisoformat(item["date"]),
                portion=Decimal(str(item["portion"])),
                counted=bool(item["counted"]),
                not_counted_reason=item.get("not_counted_reason"),
            )
            for item in self.days
        ]


def day_documents(days: list[LeaveRequestDay]) -> list[dict[str, Any]]:
    """The stored form of a request's days (portions as text, so 0.5 and 1.0 stay exact)."""
    return [
        {
            "date": day.date.isoformat(),
            "portion": str(day.portion),
            "counted": day.counted,
            "not_counted_reason": day.not_counted_reason,
        }
        for day in days
    ]


@dataclasses.dataclass(kw_only=True, eq=False)
class LeaveDecision(Record):
    __collection__ = "leave_decisions"
    __append_only__ = True
    __indexes__ = (
        Index(
            asc("organization_id", "decided_by", "idempotency_key"),
            name="uq_leave_decisions_idempotency_key",
            unique=True,
        ),
        Index(asc("organization_id", "leave_request_id"), name="ix_leave_decisions_leave_request_id"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "decision": allowed(Decision),
        "step": {"minimum": 1},
        "note": {"minLength": 1, "maxLength": 1000},
        "idempotency_key": {"minLength": 8, "maxLength": 100},
    }

    leave_request_id: uuid.UUID
    step: int = 1
    decision: str
    decided_by: uuid.UUID
    note: str | None = None
    idempotency_key: str
    decided_at: datetime = dataclasses.field(default_factory=utcnow)


YEARLY_TYPES: Final = frozenset(
    {
        EntryType.OPENING_BALANCE,
        EntryType.CARRY_FORWARD_OUT,
        EntryType.CARRY_FORWARD,
        EntryType.CARRY_FORWARD_EXPIRY,
        EntryType.LAPSE,
    }
)


@dataclasses.dataclass(kw_only=True, eq=False)
class LeaveLedgerEntry(Record):
    """Append-only. The balance is the sum of the entries."""

    __collection__ = "leave_ledger"
    __append_only__ = True
    __indexes__ = (
        Index(
            asc("organization_id", "employee_id", "leave_type_id", "effective_date"),
            name="ix_leave_ledger_balance",
        ),
        Index(
            asc("organization_id", "employee_id", "leave_type_id", "leave_year", "entry_type"),
            name="uq_leave_ledger_yearly",
            unique=True,
            partial={"slot": "yearly"},
        ),
        Index(
            asc("organization_id", "employee_id", "leave_type_id", "period_start"),
            name="uq_leave_ledger_periodic",
            unique=True,
            partial={"slot": "periodic"},
        ),
        Index(asc("organization_id", "import_batch_id"), name="ix_leave_ledger_import_batch_id"),
        Index(
            asc("organization_id", "leave_request_id", "entry_type"),
            name="uq_leave_ledger_request",
            unique=True,
            partial={"leave_request_id": {"$type": "binData"}},
        ),
        Index(
            asc("organization_id", "employee_id", "idempotency_key"),
            name="uq_leave_ledger_employee_id_idempotency_key",
            unique=True,
            partial={"idempotency_key": {"$type": "string"}},
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "entry_type": allowed(EntryType),
        "reason": {"minLength": 1, "maxLength": 500},
        "idempotency_key": {"minLength": 8, "maxLength": 100},
        "slot": allowed(("yearly", "periodic")),
    }
    __expr_rules__ = (
        {"$ne": ["$amount", 0]},
        {
            "$or": [
                {"$not": [_is("entry_type", "usage", "usage_reversal")]},
                {"$eq": [{"$multiply": ["$amount", 2]}, {"$round": [{"$multiply": ["$amount", 2]}, 0]}]},
            ]
        },
        {"$eq": [_is("entry_type", "usage", "usage_reversal"), _set("leave_request_id")]},
        {
            "$or": [
                {"$and": [_is("entry_type", "usage"), {"$lt": ["$amount", 0]}]},
                {
                    "$and": [
                        _is("entry_type", "usage_reversal", "accrual", "carry_forward"),
                        {"$gt": ["$amount", 0]},
                    ]
                },
                {
                    "$and": [
                        _is("entry_type", "carry_forward_out", "carry_forward_expiry", "lapse"),
                        {"$lt": ["$amount", 0]},
                    ]
                },
                _is("entry_type", "opening_balance", "adjustment"),
            ]
        },
        {"$or": [{"$not": [_is("entry_type", "adjustment", "opening_balance")]}, _set("reason")]},
        {
            "$or": [
                _null("period_start"),
                {"$and": [_is("entry_type", "accrual"), {"$gte": ["$period_start", "$leave_year"]}]},
            ]
        },
    )

    id: uuid.UUID = dataclasses.field(default_factory=uuid7)
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    # The first day of the leave year the entry belongs to.
    leave_year: date
    entry_type: str
    amount: Decimal
    effective_date: date
    leave_request_id: uuid.UUID | None = None
    policy_id: uuid.UUID | None = None
    reason: str | None = None
    # None only for entries the worker writes as the system (periodic accrual, expiry).
    created_by: uuid.UUID | None = None
    idempotency_key: str | None = None
    # The accrual period a periodic accrual belongs to.
    period_start: date | None = None
    # Opening balances imported together share a batch.
    import_batch_id: uuid.UUID | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    # Derived (before_write): which once-per-year or once-per-period slot the entry takes.
    slot: str | None = None

    def before_write(self) -> None:
        if self.entry_type in YEARLY_TYPES or (
            self.entry_type == EntryType.ACCRUAL and self.period_start is None
        ):
            self.slot = "yearly"
        elif self.entry_type == EntryType.ACCRUAL:
            self.slot = "periodic"
        else:
            self.slot = None
