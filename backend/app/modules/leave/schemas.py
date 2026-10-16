"""Request and response models for leave management (docs/api-architecture.md §8, Leave).

One model per audience (CLAUDE.md rule 4): request lists and the calendar never carry the
reason, which is `personal`; the request detail carries it only for the employee and for
someone who may decide the request. Day counts and balances are decimals in steps of a half,
sent as strings.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type Code = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^\S+$")
]
type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
type Text1000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
type Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
type Days = Annotated[Decimal, Field(max_digits=5, decimal_places=2, multiple_of=Decimal("0.5"))]
# Accruals, caps, opening balances and adjustments may be in hundredths of a day.
type Amount = Annotated[Decimal, Field(max_digits=5, decimal_places=2)]
type HalfValue = Literal["first", "second"]
type StatusValue = Literal["pending", "approved", "rejected", "cancelled", "cancellation_requested"]
type EmploymentTypeValue = Literal["full_time", "part_time", "contract", "intern"]
type EntryTypeValue = Literal[
    "opening_balance",
    "accrual",
    "usage",
    "usage_reversal",
    "adjustment",
    "carry_forward_out",
    "carry_forward",
    "carry_forward_expiry",
    "lapse",
]
type AccrualMethodValue = Literal["none", "upfront_per_leave_year", "periodic"]
type FrequencyValue = Literal["monthly", "quarterly"]
type TimingValue = Literal["period_start", "period_end"]
type RoundingValue = Literal["exact", "nearest_half", "down_half", "up_half"]
type CarryModeValue = Literal["none", "capped", "unlimited"]
type ExcessValue = Literal["lapse", "keep"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ref(_Response):
    id: uuid.UUID
    name: str


# --- leave types --------------------------------------------------------------------------------


class LeaveTypeCreate(_Request):
    """Every rule is stated explicitly: nothing is assumed for Sigvitas."""

    code: Code
    name: Name
    description: Text1000 | None = None
    is_paid: bool
    requires_approval: bool
    allows_half_day: bool
    counts_weekly_offs: bool
    counts_holidays: bool


class LeaveTypeUpdate(_Request):
    name: Name | None = None
    description: Text1000 | None = None
    is_paid: bool | None = None
    requires_approval: bool | None = None
    allows_half_day: bool | None = None
    counts_weekly_offs: bool | None = None
    counts_holidays: bool | None = None
    status: Literal["active", "archived"] | None = None


class LeaveTypeView(_Response):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    is_paid: bool
    requires_approval: bool
    allows_half_day: bool
    counts_weekly_offs: bool
    counts_holidays: bool
    status: Literal["active", "archived"]
    version: int
    created_at: datetime
    updated_at: datetime


class LeaveTypeList(_Response):
    items: list[LeaveTypeView]


# --- leave policies -----------------------------------------------------------------------------


class LeavePolicyCreate(_Request):
    """A new version for a scope (leave type, plus an optional location and employment type).
    The scope's open version, if it starts earlier, ends the day this one starts."""

    leave_type_id: uuid.UUID
    location_id: uuid.UUID | None = None
    employment_type: EmploymentTypeValue | None = None
    effective_from: date
    leave_year_start_month: Annotated[int, Field(ge=1, le=12)]
    leave_year_start_day: Annotated[int, Field(ge=1, le=31)]
    accrual_method: AccrualMethodValue
    # Days given each leave year (upfront) or each period (periodic); null with no accrual.
    accrual_amount: Annotated[Amount, Field(gt=0, le=366)] | None = None
    # Periodic accrual only: how often, and whether at the start or the end of each period.
    accrual_frequency: FrequencyValue | None = None
    accrual_timing: TimingValue | None = None
    # Upfront and periodic accrual: whether someone joining (or, periodic only, leaving) part
    # way through a year or period gets a share by days employed.
    prorate_on_joining: bool | None = None
    prorate_on_exit: bool | None = None
    # How a prorated share is rounded; required when either proration is on.
    proration_rounding: RoundingValue | None = None
    allow_negative_balance: bool
    # With a negative balance allowed: how far below zero it may go.
    max_negative_days: Annotated[Days, Field(ge=0)] | None = None
    # The year end. Null: not decided yet (the year-end run skips this policy).
    carry_forward_mode: CarryModeValue | None = None
    # Capped carry-forward: the most days carried into the next leave year.
    carry_forward_cap: Annotated[Amount, Field(ge=0)] | None = None
    # With nothing or a capped amount carried: whether the rest lapses or stays.
    excess_action: ExcessValue | None = None
    # Carried days not used within this many months of the new leave year expire.
    carried_expire_after_months: Annotated[int, Field(ge=1, le=24)] | None = None


class LeavePolicyView(_Response):
    id: uuid.UUID
    leave_type: Ref
    location: Ref | None
    employment_type: EmploymentTypeValue | None
    effective_from: date
    effective_to: date | None
    leave_year_start_month: int
    leave_year_start_day: int
    accrual_method: AccrualMethodValue
    accrual_amount: Decimal | None
    accrual_frequency: FrequencyValue | None
    accrual_timing: TimingValue | None
    prorate_on_joining: bool | None
    prorate_on_exit: bool | None
    proration_rounding: RoundingValue | None
    allow_negative_balance: bool
    max_negative_days: Decimal | None
    carry_forward_mode: CarryModeValue | None
    carry_forward_cap: Decimal | None
    excess_action: ExcessValue | None
    carried_expire_after_months: int | None
    created_at: datetime


class LeavePolicyList(_Response):
    items: list[LeavePolicyView]


# --- balances and the ledger --------------------------------------------------------------------


class BalanceView(_Response):
    leave_type: Ref
    is_paid: bool
    allows_half_day: bool
    requires_approval: bool
    # Sum of every ledger entry.
    balance: Decimal
    # Days in pending requests.
    reserved: Decimal
    # balance - reserved: what a new request can use.
    available: Decimal
    # A policy covers the employee for this type today; without one no request can be made.
    eligible: bool
    allow_negative_balance: bool
    max_negative_days: Decimal | None


class BalanceList(_Response):
    items: list[BalanceView]


class LedgerEntryView(_Response):
    id: uuid.UUID
    leave_type: Ref
    leave_year: date
    entry_type: EntryTypeValue
    amount: Decimal
    effective_date: date
    leave_request_id: uuid.UUID | None
    # The accrual period of a periodic accrual.
    period_start: date | None
    reason: str | None
    created_at: datetime


class LedgerList(_Response):
    items: list[LedgerEntryView]


class AdjustmentCreate(_Request):
    leave_type_id: uuid.UUID
    entry_type: Literal["adjustment", "opening_balance"]
    amount: Amount
    effective_date: date
    reason: Reason


class AllocationRun(_Request):
    leave_type_id: uuid.UUID
    # Any date in the leave year to allocate: each employee's policy on that date decides it.
    as_of: date


class AllocationResult(_Response):
    allocated: int
    already_allocated: int
    no_policy: int
    no_allocation_in_policy: int
    skipped_own: int
    # Joined so late in the year that the prorated share rounds to nothing.
    nothing_due: int


class YearEndRun(_Request):
    leave_type_id: uuid.UUID
    # The last day of the leave year to close; it must have passed.
    year_end: date
    # False shows what would happen without recording anything.
    apply: bool


class YearEndRowView(_Response):
    employee: Ref
    # done | would_do | already_closed | no_policy | not_decided | not_year_end | skipped_own
    outcome: str
    closing: Decimal | None
    carried: Decimal | None
    lapsed: Decimal | None
    kept: Decimal | None


class YearEndResult(_Response):
    applied: bool
    counts: dict[str, int]
    items: list[YearEndRowView]


class OpeningBalanceRow(_Request):
    employee_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    leave_type_code: Code
    days: Amount
    effective_date: date


class OpeningBalanceImport(_Request):
    reason: Reason
    rows: Annotated[list[OpeningBalanceRow], Field(min_length=1, max_length=5000)]
    # False checks every row without recording anything.
    apply: bool


class ImportRowView(_Response):
    line: int
    employee_code: str
    leave_type_code: str
    # ok | recorded | unknown_employee | unknown_leave_type | not_permitted | own_balance |
    # no_policy | already_recorded | duplicate_row | zero_days
    outcome: str


class ImportResult(_Response):
    applied: bool
    batch_id: uuid.UUID | None
    counts: dict[str, int]
    items: list[ImportRowView]


# --- requests -----------------------------------------------------------------------------------


class RequestDraft(_Request):
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    start_half: HalfValue | None = None
    end_half: HalfValue | None = None


class RequestCreate(RequestDraft):
    reason: Text1000 | None = None


class LeaveDayView(_Response):
    date: date
    portion: Decimal
    counted: bool
    not_counted_reason: Literal["holiday", "weekly_off"] | None


class PreviewView(_Response):
    days: list[LeaveDayView]
    days_requested: Decimal
    available_before: Decimal
    available_after: Decimal
    within_balance: bool
    requires_approval: bool
    # No weekly offs are configured yet (shifts arrive with M3): every non-holiday day counts.
    weekly_offs_configured: bool


class RequestSummary(_Response):
    """For lists and inboxes: no reason."""

    id: uuid.UUID
    employee: Ref
    leave_type: Ref
    start_date: date
    end_date: date
    start_half: HalfValue | None
    end_half: HalfValue | None
    days_requested: Decimal
    status: StatusValue
    created_at: datetime


class RequestPage(_Response):
    items: list[RequestSummary]
    next_cursor: str | None


class DecisionView(_Response):
    decision: Literal["approved", "rejected", "cancellation_approved", "cancellation_rejected"]
    decided_by: str
    note: str | None
    decided_at: datetime


class RequestDetail(RequestSummary):
    # Only for the employee and for someone who may decide the request.
    reason: str | None
    reason_visible: bool
    days: list[LeaveDayView]
    decisions: list[DecisionView]
    can_decide: bool
    can_cancel: bool
    version: int


class Decide(_Request):
    note: Text1000 | None = None


class LeaveCalendarEntry(_Response):
    """Who is away: no reason, ever (product-requirements LV-8)."""

    request_id: uuid.UUID
    employee: Ref
    leave_type: Ref
    start_date: date
    end_date: date
    start_half: HalfValue | None
    end_half: HalfValue | None
    status: Literal["pending", "approved", "cancellation_requested"]


class LeaveCalendarView(_Response):
    items: list[LeaveCalendarEntry]
    # More entries than the limit: narrow the range or the filters.
    truncated: bool


# --- report -------------------------------------------------------------------------------------


class LeaveReportRow(_Response):
    employee: Ref
    employee_code: str
    department: str | None
    location: str | None
    leave_type: Ref
    # Counted days of approved leave in the period, and of requests still waiting.
    taken: Decimal
    pending: Decimal


class LeaveReportView(_Response):
    start: date
    end: date
    items: list[LeaveReportRow]
    next_cursor: str | None
