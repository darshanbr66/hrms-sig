"""Judged attendance days for a set of employees over a date range.

Day summaries are derived when read, from the events and the configuration effective on each
day (docs/database-design.md §4.5, `derive_day`), rather than kept in a stored projection:
an event, a correction, a shift or policy version, leave or a holiday changing is reflected at
once (ATT-10), and nothing can go stale. Reads are bounded (one date for the register, at most
`MAX_RANGE_DAYS` days elsewhere).
"""

import uuid
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from app.modules.attendance import timeline
from app.modules.attendance.derive import DayContext, DaySummary, derive_day
from app.modules.attendance.models import (
    AttendanceCorrection,
    AttendanceEvent,
    AttendanceEventVoid,
    CorrectionStatus,
)
from app.modules.attendance.schedule import Schedule, policy_values, shift_times
from app.modules.attendance.timeline import EventType
from app.modules.leave import public as leave
from app.modules.org import public as org
from app.platform.db import Session

MAX_RANGE_DAYS: Final = 93
UTC: Final = ZoneInfo("UTC")
NO_LEAVE: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class DayEvent:
    event: AttendanceEvent
    voided: bool


@dataclass(frozen=True, slots=True)
class JudgedDay:
    employee_id: uuid.UUID
    work_date: date
    time_zone: str
    events: list[DayEvent]
    # Clocked-in and break time to the second, an open session counted up to now.
    totals: timeline.Totals
    summary: DaySummary
    shift_name: str | None
    holiday: bool
    leave_portion: Decimal

    @property
    def notable(self) -> bool:
        """Worth listing in a history: it has events, a status, leave or a holiday."""
        return bool(self.events) or self.summary.status is not None or self.holiday or self.leave_portion > 0


def dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


async def voided_ids(session: Session, event_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    if not event_ids:
        return set()
    return set(
        await session.distinct(AttendanceEventVoid, "event_id", {"event_id": {"$in": list(set(event_ids))}})
    )


def effective(events: Sequence[DayEvent]) -> list[timeline.Event]:
    return [
        timeline.Event(EventType(e.event.event_type), e.event.occurred_at) for e in events if not e.voided
    ]


async def judge(
    session: Session, employee_ids: Collection[uuid.UUID], start: date, end: date, now: datetime
) -> dict[uuid.UUID, list[JudgedDay]]:
    """Every date from `start` to `end` (inclusive) for each employee, oldest first."""
    ids = set(employee_ids)
    if not ids:
        return {}
    schedule = await Schedule.load(session, ids, start, end)
    location_ids = {job.location_id for e in schedule.employments.values() for job in e.jobs}
    zones = await org.location_time_zones(session, location_ids)
    rows = await session.find(
        AttendanceEvent,
        {"employee_id": {"$in": list(ids)}, "work_date": {"$gte": start, "$lte": end}},
        sort=[("occurred_at", 1), ("id", 1)],
    )
    voided = await voided_ids(session, [row.id for row in rows])
    by_day: dict[tuple[uuid.UUID, date], list[DayEvent]] = defaultdict(list)
    for row in rows:
        by_day[(row.employee_id, row.work_date)].append(DayEvent(row, row.id in voided))
    holidays = await leave.holiday_dates(session, location_ids, start, end)
    working_days = await leave.working_day_dates(session, location_ids, start, end)
    portions = await leave.leave_portions(session, ids, start, end)
    pending = {
        (row.employee_id, row.work_date)
        for row in await session.find(
            AttendanceCorrection,
            {
                "employee_id": {"$in": list(ids)},
                "status": CorrectionStatus.PENDING.value,
                "work_date": {"$gte": start, "$lte": end},
            },
        )
    }

    judged: dict[uuid.UUID, list[JudgedDay]] = {}
    for employee_id in ids:
        employment = schedule.employments.get(employee_id)
        days = []
        for day in dates(start, end):
            location_id = schedule.location_on(employee_id, day)
            zone_name = zones.get(location_id, "UTC") if location_id else "UTC"
            applied = schedule.applied(employee_id, day)
            events = by_day.get((employee_id, day), [])
            counted = effective(events)
            holiday = location_id is not None and day in holidays.get(location_id, set())
            portion = portions.get((employee_id, day), NO_LEAVE)
            context = DayContext(
                work_date=day,
                zone=ZoneInfo(zone_name),
                shift=shift_times(applied.version) if applied else None,
                policy=policy_values(applied.policy) if applied and applied.policy else None,
                holiday=holiday,
                declared_working_day=location_id is not None and day in working_days.get(location_id, set()),
                leave_portion=portion,
                pending_correction=(employee_id, day) in pending,
                employed=employment is not None and employment.employed_on(day),
            )
            days.append(
                JudgedDay(
                    employee_id=employee_id,
                    work_date=day,
                    time_zone=zone_name,
                    events=events,
                    totals=timeline.totals(counted, now),
                    summary=derive_day(counted, context, now),
                    shift_name=applied.shift.name if applied else None,
                    holiday=holiday,
                    leave_portion=portion,
                )
            )
        judged[employee_id] = days
    return judged
