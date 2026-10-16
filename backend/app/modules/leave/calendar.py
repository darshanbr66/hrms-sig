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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leave.models import (
    Holiday,
    HolidayCalendar,
    HolidayKind,
    LeaveRequest,
    LeaveRequestDay,
    LocationHolidayCalendar,
    RequestStatus,
)

# Approved leave keeps its days while a cancellation waits for a decision.
_TAKEN = (RequestStatus.APPROVED.value, RequestStatus.CANCELLATION_REQUESTED.value)


async def holiday_dates(
    session: AsyncSession, location_ids: Collection[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, set[date]]:
    """Holidays (not optional ones) of the active calendar assigned to each location for the
    year, between `start` and `end` inclusive."""
    return await _entry_dates(session, HolidayKind.HOLIDAY, location_ids, start, end)


async def working_day_dates(
    session: AsyncSession, location_ids: Collection[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, set[date]]:
    """Declared working days of the active calendar assigned to each location for the year,
    between `start` and `end` inclusive."""
    return await _entry_dates(session, HolidayKind.WORKING_DAY, location_ids, start, end)


async def _entry_dates(
    session: AsyncSession,
    kind: HolidayKind,
    location_ids: Collection[uuid.UUID],
    start: date,
    end: date,
) -> dict[uuid.UUID, set[date]]:
    found: dict[uuid.UUID, set[date]] = defaultdict(set)
    if not location_ids:
        return found
    rows = await session.execute(
        select(LocationHolidayCalendar.location_id, Holiday.date)
        .join(HolidayCalendar, HolidayCalendar.id == LocationHolidayCalendar.calendar_id)
        .join(Holiday, Holiday.calendar_id == HolidayCalendar.id)
        .where(
            LocationHolidayCalendar.location_id.in_(set(location_ids)),
            HolidayCalendar.status == "active",
            Holiday.kind == kind.value,
            Holiday.is_optional.is_(False),
            Holiday.date >= start,
            Holiday.date <= end,
        )
    )
    for location_id, day in rows.all():
        found[location_id].add(day)
    return found


async def leave_portions(
    session: AsyncSession, employee_ids: Collection[uuid.UUID], start: date, end: date
) -> dict[tuple[uuid.UUID, date], Decimal]:
    """The portion of each day (0.5 or 1) covered by approved leave, per employee and date."""
    if not employee_ids:
        return {}
    rows = await session.execute(
        select(LeaveRequest.employee_id, LeaveRequestDay.date, LeaveRequestDay.portion)
        .join(LeaveRequestDay, LeaveRequestDay.leave_request_id == LeaveRequest.id)
        .where(
            LeaveRequest.employee_id.in_(set(employee_ids)),
            LeaveRequest.status.in_(_TAKEN),
            LeaveRequestDay.date >= start,
            LeaveRequestDay.date <= end,
        )
    )
    portions: dict[tuple[uuid.UUID, date], Decimal] = {}
    for employee_id, day, portion in rows.all():
        key = (employee_id, day)
        portions[key] = min(Decimal("1"), portions.get(key, Decimal("0")) + Decimal(portion))
    return portions
