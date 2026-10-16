"""What a correction may change, as a pure function (product-requirements ATT-3, ATT-6, ATT-7).

A correction concerns one work date: it adds events at proposed times and voids existing
ones. It is accepted only if the day it produces is a valid sequence and stays apart from the
employee's other days:

- something must change; an event is voided at most once, and only an effective event of
  that work date can be voided;
- an added event's time is in the past, falls on the work date or the next calendar day in the
  employee's time zone (a session crossing midnight), and is not at the same moment as another
  event of the day;
- every event of the resulting day comes after the last event of earlier work dates and before
  the first event of later ones, so days never overlap;
- the resulting day follows the clocking sequence from clocked out (no break without clocking
  in, no second clock-in while clocked in), and ends clocked out unless no later event exists
  (an open session may stay open; the system never invents a clock-out).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from app.modules.attendance import timeline
from app.modules.attendance.timeline import EventType, SequenceError, State


class CorrectionRuleError(Exception):
    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Existing:
    id: uuid.UUID
    event_type: EventType
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Add:
    event_type: EventType
    proposed_at: datetime


def resulting_day(
    existing: Sequence[Existing],
    adds: Sequence[Add],
    voids: Sequence[uuid.UUID],
    *,
    work_date: date,
    zone: tzinfo,
    earlier_last: datetime | None,
    later_first: datetime | None,
    now: datetime,
) -> list[timeline.Event]:
    """The work date's effective events after the correction, or CorrectionRuleError."""
    if not adds and not voids:
        raise CorrectionRuleError("items", "correction.empty", "Add or remove at least one event.")
    if len(set(voids)) != len(voids):
        raise CorrectionRuleError("items", "correction.void_twice", "Remove each event only once.")
    known = {event.id for event in existing}
    if any(target not in known for target in voids):
        raise CorrectionRuleError(
            "items",
            "correction.unknown_event",
            "One of the events to remove is not part of this day, or was already removed. Reload the day.",
        )
    allowed_dates = {work_date, work_date + timedelta(days=1)}
    for add in adds:
        if add.proposed_at > now:
            raise CorrectionRuleError(
                "items", "correction.in_future", "Choose times that have already passed."
            )
        if add.proposed_at.astimezone(zone).date() not in allowed_dates:
            raise CorrectionRuleError(
                "items",
                "correction.outside_day",
                "Choose times on this work date, or early the next day for a session that crossed midnight.",
            )
    removed = set(voids)
    events = [
        timeline.Event(event.event_type, event.occurred_at) for event in existing if event.id not in removed
    ] + [timeline.Event(add.event_type, add.proposed_at) for add in adds]
    events.sort(key=lambda event: event.occurred_at)
    moments = [event.occurred_at for event in events]
    if len(set(moments)) != len(moments):
        raise CorrectionRuleError(
            "items", "correction.same_time", "Two events cannot happen at the same moment."
        )
    if events and earlier_last is not None and events[0].occurred_at <= earlier_last:
        raise CorrectionRuleError(
            "items", "correction.overlaps_earlier", "The day would overlap the previous recorded day."
        )
    if events and later_first is not None and events[-1].occurred_at >= later_first:
        raise CorrectionRuleError(
            "items", "correction.overlaps_later", "The day would overlap the next recorded day."
        )
    state = State.CLOCKED_OUT
    for event in events:
        try:
            state = timeline.next_state(state, event.event_type)
        except SequenceError as exc:
            raise CorrectionRuleError(
                "items",
                "correction.sequence",
                f"The day would not make sense: {exc.event_type.value.replace('_', ' ').lower()} "
                f"cannot follow while {exc.state.value.replace('_', ' ')}.",
            ) from exc
    if state is not State.CLOCKED_OUT and later_first is not None:
        raise CorrectionRuleError(
            "items", "correction.unclosed", "Add a clock-out: a later day already has events."
        )
    return events
