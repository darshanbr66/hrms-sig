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
type HalfValue = Literal["first", "second"]
type StatusValue = Literal["pending", "approved", "rejected", "cancelled", "cancellation_requested"]
type EmploymentTypeValue = Literal["full_time", "part_time", "contract", "intern"]
type EntryTypeValue = Literal["opening_balance", "accrual", "usage", "usage_reversal", "adjustment"]


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
    accrual_method: Literal["none", "upfront_per_leave_year"]
    # Days given at the start of each leave year (upfront); null with no allocation.
    accrual_amount: Annotated[Days, Field(gt=0)] | None = None
    allow_negative_balance: bool
    # With a negative balance allowed: how far below zero it may go.
    max_negative_days: Annotated[Days, Field(ge=0)] | None = None


class LeavePolicyView(_Response):
    id: uuid.UUID
    leave_type: Ref
    location: Ref | None
    employment_type: EmploymentTypeValue | None
    effective_from: date
    effective_to: date | None
    leave_year_start_month: int
    leave_year_start_day: int
    accrual_method: Literal["none", "upfront_per_leave_year"]
    accrual_amount: Decimal | None
    allow_negative_balance: bool
    max_negative_days: Decimal | None
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
    reason: str | None
    created_at: datetime


class LedgerList(_Response):
    items: list[LedgerEntryView]


class AdjustmentCreate(_Request):
    leave_type_id: uuid.UUID
    entry_type: Literal["adjustment", "opening_balance"]
    amount: Days
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
