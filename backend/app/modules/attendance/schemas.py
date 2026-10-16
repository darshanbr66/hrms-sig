"""Request and response models for attendance (docs/api-architecture.md §8, Attendance).

Durations are measured on the server (CLAUDE.md rule 14); the client formats them: clocked-in
and break time in whole seconds, judged values (effective, late, early, overtime) in whole
minutes. Times are UTC; `time_zone` is the employee's location zone for display.
"""

import uuid
from datetime import date, datetime, time
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

type EventTypeValue = Literal["CLOCK_IN", "BREAK_START", "BREAK_END", "CLOCK_OUT"]
type StateValue = Literal["clocked_out", "clocked_in", "on_break"]
type DayStatusValue = Literal[
    "present", "half_day", "absent", "on_leave", "holiday", "weekly_off", "incomplete", "pending_correction"
]
type CorrectionStatusValue = Literal["pending", "approved", "rejected", "cancelled"]
type Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
type Code = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^\S+$")
]
type Minutes = Annotated[int, Field(ge=0, le=1440)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: EventTypeValue
    # The device's clock, kept as metadata only; the server's time is the event time.
    client_reported_at: AwareDatetime | None = None


class EventView(_Response):
    id: uuid.UUID
    event_type: EventTypeValue
    occurred_at: datetime
    source: Literal["web", "mobile", "correction", "import"]
    # Removed by an approved correction: shown, but no longer counted.
    voided: bool
    correction_id: uuid.UUID | None


class DayView(_Response):
    work_date: date
    first_in: datetime | None
    last_out: datetime | None
    # Time between clocking in and out, including breaks; an open session counts up to now.
    session_seconds: int
    break_seconds: int
    open_session: bool
    on_break: bool
    # Clocked out during a break, so the break's end was not recorded.
    missing_break_end: bool
    # The day as judged by the shift and attendance policy effective on it; null until the
    # day has closed, or when no shift or policy applies (`judged` is then false).
    status: DayStatusValue | None
    judged: bool
    shift_name: str | None
    shift_start_at: datetime | None
    shift_end_at: datetime | None
    effective_minutes: int
    late_minutes: int
    early_leave_minutes: int
    overtime_minutes: int
    anomalies: list[str]
    holiday: bool
    # Approved leave on the day: "0", "0.5" or "1".
    leave_portion: str
    events: list[EventView]


class TodayView(_Response):
    state: StateValue
    work_date: date
    time_zone: str
    server_time: datetime
    allowed_events: list[EventTypeValue]
    # The work date of a session left open from an earlier day (a forgotten clock-out).
    open_since: date | None
    day: DayView


class DayList(_Response):
    time_zone: str
    items: list[DayView]


class TeamMemberRef(_Response):
    id: uuid.UUID
    name: str
    employee_code: str
    department: str | None
    designation: str | None
    location: str | None


class TeamMemberToday(_Response):
    employee: TeamMemberRef
    state: StateValue
    work_date: date
    first_in: datetime | None
    last_event_at: datetime | None
    session_seconds: int
    break_seconds: int


class TeamSummaryView(_Response):
    # Employed people whose attendance the viewer may read.
    in_scope: int
    clocked_in_today: int
    working_now: int
    on_break: int


class TeamTodayView(_Response):
    summary: TeamSummaryView
    items: list[TeamMemberToday]
    next_cursor: str | None


# --- register and report ---------------------------------------------------------------------


class RegisterEntryView(_Response):
    employee: TeamMemberRef
    time_zone: str
    day: DayView


class RegisterView(_Response):
    date: date
    in_scope: int
    # Status -> people; `not_judged` for days not judged yet or without a shift and policy.
    counts: dict[str, int]
    items: list[RegisterEntryView]
    next_cursor: str | None


class AttendanceReportRow(_Response):
    employee: TeamMemberRef
    # Status -> days.
    days: dict[str, int]
    not_judged_days: int
    effective_minutes: int
    late_days: int
    late_minutes: int
    early_leave_days: int
    overtime_minutes: int


class AttendanceReportView(_Response):
    start: date
    end: date
    items: list[AttendanceReportRow]
    next_cursor: str | None


# --- corrections -----------------------------------------------------------------------------


class CorrectionItemCreate(_Request):
    action: Literal["add", "void"]
    event_type: EventTypeValue | None = None
    proposed_at: AwareDatetime | None = None
    target_event_id: uuid.UUID | None = None


class CorrectionCreate(_Request):
    work_date: date
    reason: Reason
    items: list[CorrectionItemCreate] = Field(min_length=1, max_length=20)


class CorrectionDecision(_Request):
    note: Reason | None = None


class CorrectionItemView(_Response):
    action: Literal["add", "void"]
    event_type: EventTypeValue | None
    proposed_at: datetime | None
    target_event_id: uuid.UUID | None


class TimelineEventView(_Response):
    event_type: EventTypeValue
    occurred_at: datetime


class CorrectionSummary(_Response):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    work_date: date
    kind: Literal["request", "hr_recorded"]
    status: CorrectionStatusValue
    requested_at: datetime
    decided_at: datetime | None
    locked_period: bool


class CorrectionDetailView(_Response):
    correction: CorrectionSummary
    # Shown only to the employee, to whoever may decide, and to HR who recorded it.
    reason: str | None
    decision_note: str | None
    requested_by: str
    decided_by: str | None
    items: list[CorrectionItemView]
    time_zone: str
    before: list[TimelineEventView]
    after: list[TimelineEventView]
    # For a waiting correction: whether it still fits the day as recorded now.
    still_valid: bool
    can_decide: bool
    can_cancel: bool
    version: int


class CorrectionList(_Response):
    items: list[CorrectionSummary]
    next_cursor: str | None


# --- configuration ---------------------------------------------------------------------------


class WeeklyOffValue(_Request):
    # Monday is 1, Sunday 7.
    weekday: Annotated[int, Field(ge=1, le=7)]
    # Weeks of the month (the first such weekday is week 1); null for every week.
    weeks: Annotated[list[Annotated[int, Field(ge=1, le=5)]], Field(min_length=1, max_length=5)] | None = None


class ShiftVersionCreate(_Request):
    effective_from: date
    start_time: time
    end_time: time
    weekly_offs: Annotated[list[WeeklyOffValue], Field(max_length=7)]


class ShiftCreate(_Request):
    code: Code
    name: Name
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None
    ) = None
    version: ShiftVersionCreate


class ShiftUpdate(_Request):
    name: Name | None = None
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None
    ) = None
    status: Literal["active", "archived"] | None = None


class WeeklyOffView(_Response):
    weekday: int
    weeks: list[int] | None


class ShiftVersionView(_Response):
    id: uuid.UUID
    version_no: int
    effective_from: date
    effective_to: date | None
    start_time: time
    end_time: time
    crosses_midnight: bool
    weekly_offs: list[WeeklyOffView]


class ShiftView(_Response):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    status: Literal["active", "archived"]
    version: int
    versions: list[ShiftVersionView]


class ShiftList(_Response):
    items: list[ShiftView]


class DefaultShiftCreate(_Request):
    location_id: uuid.UUID
    shift_id: uuid.UUID
    effective_from: date


class DefaultShiftView(_Response):
    id: uuid.UUID
    location_id: uuid.UUID
    location_name: str
    shift_id: uuid.UUID
    shift_name: str
    effective_from: date
    effective_to: date | None


class DefaultShiftList(_Response):
    items: list[DefaultShiftView]


class ShiftAssignmentCreate(_Request):
    employee_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=500)]
    shift_id: uuid.UUID
    effective_from: date


class ShiftAssignmentResult(_Response):
    assigned: int
    # People outside the caller's assignment scope; nothing was changed for them.
    skipped_not_permitted: int


class ShiftAssignmentView(_Response):
    id: uuid.UUID
    shift_id: uuid.UUID
    shift_name: str
    effective_from: date
    effective_to: date | None


class ShiftAssignmentList(_Response):
    items: list[ShiftAssignmentView]


class PolicyCreate(_Request):
    scope_type: Literal["organization", "location", "shift"]
    scope_id: uuid.UUID | None = None
    effective_from: date
    grace_minutes: Minutes
    early_departure_grace_minutes: Minutes
    duration_basis: Literal["effective", "gross"]
    full_day_min_minutes: Annotated[int, Field(ge=1, le=1440)]
    half_day_min_minutes: Annotated[int, Field(ge=1, le=1440)]
    max_break_minutes: Minutes | None = None
    breaks_count_as_work: bool
    break_excess_action: Literal["flag", "deduct"]
    overtime_enabled: bool
    overtime_basis: Literal["beyond_full_day", "beyond_shift_end"] | None = None
    overtime_min_minutes: Minutes | None = None
    overtime_requires_approval: bool
    incomplete_after_minutes: Minutes
    allow_multiple_sessions: bool


class PolicyView(_Response):
    id: uuid.UUID
    scope_type: Literal["organization", "location", "shift"]
    scope_id: uuid.UUID | None
    scope_name: str | None
    effective_from: date
    effective_to: date | None
    grace_minutes: int
    early_departure_grace_minutes: int
    duration_basis: Literal["effective", "gross"]
    full_day_min_minutes: int
    half_day_min_minutes: int
    max_break_minutes: int | None
    breaks_count_as_work: bool
    break_excess_action: Literal["flag", "deduct"]
    overtime_enabled: bool
    overtime_basis: Literal["beyond_full_day", "beyond_shift_end"] | None
    overtime_min_minutes: int | None
    overtime_requires_approval: bool
    incomplete_after_minutes: int
    allow_multiple_sessions: bool


class PolicyList(_Response):
    items: list[PolicyView]


class LockCreate(_Request):
    period_start: date
    period_end: date
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class LockRelease(_Request):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class LockView(_Response):
    id: uuid.UUID
    period_start: date
    period_end: date
    reason: str
    locked_at: datetime
    released_at: datetime | None
    release_reason: str | None


class LockList(_Response):
    items: list[LockView]


class SetupView(_Response):
    date: date
    active_shifts: int
    policies_in_effect: int
    organization_policy: bool
    employees_in_scope: int
    without_shift: list[TeamMemberRef]
    without_policy: list[TeamMemberRef]
