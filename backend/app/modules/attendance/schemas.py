"""Request and response models for attendance (docs/api-architecture.md §8, Attendance).

Durations are whole seconds measured on the server (CLAUDE.md rule 14); the client formats
them. Times are UTC; `time_zone` is the employee's location zone for display.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

type EventTypeValue = Literal["CLOCK_IN", "BREAK_START", "BREAK_END", "CLOCK_OUT"]
type StateValue = Literal["clocked_out", "clocked_in", "on_break"]


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
