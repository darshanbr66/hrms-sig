"""Calendar facts the attendance module judges days with (docs/database-design.md §4.5,
precedence: approved leave, then holidays, then weekly offs). Dates and portions only: never a
leave type's name, a reason or a note.

A holiday calendar holds holidays and declared working days (revision 0027). A declared
working day cancels the weekly off of the shift on that date only."""

import uuid
from collections import defaultdict
from collections.abc import Collection
from datetime import date
from decimal import Decimal

from app.modules.leave.models import (
    Holiday,
    HolidayCalendar,
    HolidayKind,
    LeaveRequest,
    LocationHolidayCalendar,
    RequestStatus,
)
from app.platform.db import Session

# Approved leave keeps its days while a cancellation waits for a decision.
_TAKEN = (RequestStatus.APPROVED.value, RequestStatus.CANCELLATION_REQUESTED.value)


async def holiday_dates(
    session: Session, location_ids: Collection[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, set[date]]:
    """Holidays (not optional ones) of the active calendar assigned to each location for the
    year, between `start` and `end` inclusive."""
    return await _entry_dates(session, HolidayKind.HOLIDAY, location_ids, start, end)


async def working_day_dates(
    session: Session, location_ids: Collection[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, set[date]]:
    """Declared working days of the active calendar assigned to each location for the year,
    between `start` and `end` inclusive."""
    return await _entry_dates(session, HolidayKind.WORKING_DAY, location_ids, start, end)


async def _entry_dates(
    session: Session,
    kind: HolidayKind,
    location_ids: Collection[uuid.UUID],
    start: date,
    end: date,
) -> dict[uuid.UUID, set[date]]:
    found: dict[uuid.UUID, set[date]] = defaultdict(set)
    if not location_ids:
        return found
    assignments = await session.find(
        LocationHolidayCalendar,
        {"location_id": {"$in": list(set(location_ids))}, "year": {"$gte": start.year, "$lte": end.year}},
    )
    calendars = {
        calendar.id
        for calendar in await session.find(
            HolidayCalendar,
            {"id": {"$in": list({a.calendar_id for a in assignments})}, "status": "active"},
        )
    }
    by_calendar: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for assignment in assignments:
        if assignment.calendar_id in calendars:
            by_calendar[assignment.calendar_id].add(assignment.location_id)
    if not by_calendar:
        return found
    holidays = await session.find(
        Holiday,
        {
            "calendar_id": {"$in": list(by_calendar)},
            "kind": kind.value,
            "is_optional": False,
            "date": {"$gte": start, "$lte": end},
        },
    )
    for holiday in holidays:
        for location_id in by_calendar[holiday.calendar_id]:
            found[location_id].add(holiday.date)
    return found


async def leave_portions(
    session: Session, employee_ids: Collection[uuid.UUID], start: date, end: date
) -> dict[tuple[uuid.UUID, date], Decimal]:
    """The portion of each day (0.5 or 1) covered by approved leave, per employee and date."""
    if not employee_ids:
        return {}
    requests = await session.find(
        LeaveRequest,
        {
            "employee_id": {"$in": list(set(employee_ids))},
            "status": {"$in": list(_TAKEN)},
            "start_date": {"$lte": end},
            "end_date": {"$gte": start},
        },
    )
    portions: dict[tuple[uuid.UUID, date], Decimal] = {}
    for request in requests:
        for day in request.day_rows():
            if start <= day.date <= end:
                key = (request.employee_id, day.date)
                portions[key] = min(Decimal("1"), portions.get(key, Decimal("0")) + day.portion)
    return portions
