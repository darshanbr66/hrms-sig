"""What a sequence of attendance events means, without any attendance policy.

This is mechanism only (docs/database-design.md §4.5, product-requirements §6.12): the order
events may come in, and the time between them. It says nothing about whether a day is a
full day, late, or absent, and it does not decide whether breaks count as work; those come
from Sigvitas's shift and attendance policy (`derive_day`, when policies exist).

- `next_state` applies one event to the current state, or refuses it (ATT-3: no break without
  clocking in, no second clock-in while clocked in). Clocking out during a break ends the break
  and the session, and the break is reported as not ended (`missing_break_end`).
- `totals` measures clocked-in time and break time from a day's events, counting an open
  session or break up to `now`. Nothing is invented: an open session stays open.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from enum import StrEnum
from typing import Final


class EventType(StrEnum):
    CLOCK_IN = "CLOCK_IN"
    BREAK_START = "BREAK_START"
    BREAK_END = "BREAK_END"
    CLOCK_OUT = "CLOCK_OUT"


class State(StrEnum):
    CLOCKED_OUT = "clocked_out"
    CLOCKED_IN = "clocked_in"
    ON_BREAK = "on_break"


ALLOWED: Final[dict[State, frozenset[EventType]]] = {
    State.CLOCKED_OUT: frozenset({EventType.CLOCK_IN}),
    State.CLOCKED_IN: frozenset({EventType.BREAK_START, EventType.CLOCK_OUT}),
    State.ON_BREAK: frozenset({EventType.BREAK_END, EventType.CLOCK_OUT}),
}

_AFTER: Final[dict[EventType, State]] = {
    EventType.CLOCK_IN: State.CLOCKED_IN,
    EventType.BREAK_START: State.ON_BREAK,
    EventType.BREAK_END: State.CLOCKED_IN,
    EventType.CLOCK_OUT: State.CLOCKED_OUT,
}


class SequenceError(ValueError):
    def __init__(self, state: State, event_type: EventType) -> None:
        super().__init__(f"{event_type} is not allowed while {state}")
        self.state = state
        self.event_type = event_type


def next_state(state: State, event_type: EventType) -> State:
    if event_type not in ALLOWED[state]:
        raise SequenceError(state, event_type)
    return _AFTER[event_type]


def state_after(last_event_type: EventType | None) -> State:
    return State.CLOCKED_OUT if last_event_type is None else _AFTER[last_event_type]


@dataclass(frozen=True, slots=True)
class Event:
    event_type: EventType
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Totals:
    first_in: datetime | None
    last_out: datetime | None
    session_seconds: int
    break_seconds: int
    open_session: bool
    on_break: bool
    missing_break_end: bool


def totals(events: Sequence[Event], now: datetime) -> Totals:
    """Clocked-in and break time over events in time order. An open session or break counts
    up to `now`; an event that breaks the sequence (from imported or corrected data) is skipped,
    so totals never count the same time twice."""
    state = State.CLOCKED_OUT
    first_in: datetime | None = None
    last_out: datetime | None = None
    session_start: datetime | None = None
    break_start: datetime | None = None
    session_seconds = break_seconds = 0
    missing_break_end = False
    for event in sorted(events, key=lambda item: item.occurred_at):
        if event.event_type not in ALLOWED[state]:
            continue
        match event.event_type:
            case EventType.CLOCK_IN:
                session_start = event.occurred_at
                first_in = first_in or event.occurred_at
            case EventType.BREAK_START:
                break_start = event.occurred_at
            case EventType.BREAK_END if break_start is not None:
                break_seconds += _seconds(break_start, event.occurred_at)
                break_start = None
            case EventType.CLOCK_OUT if session_start is not None:
                if break_start is not None:
                    break_seconds += _seconds(break_start, event.occurred_at)
                    break_start = None
                    missing_break_end = True
                session_seconds += _seconds(session_start, event.occurred_at)
                session_start = None
                last_out = event.occurred_at
            case _:
                pass
        state = _AFTER[event.event_type]
    if session_start is not None:
        session_seconds += _seconds(session_start, now)
    if break_start is not None:
        break_seconds += _seconds(break_start, now)
    return Totals(
        first_in=first_in,
        last_out=last_out,
        session_seconds=session_seconds,
        break_seconds=break_seconds,
        open_session=session_start is not None,
        on_break=break_start is not None,
        missing_break_end=missing_break_end,
    )


def _seconds(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds()))


def local_date(moment: datetime, time_zone: tzinfo) -> date:
    return moment.astimezone(time_zone).date()
