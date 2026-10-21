"""The attendance module's interface for other modules and the worker (docs/architecture.md §4).

- `weekly_off_dates` gives the dates in a range that are weekly offs under the shift that
  applies to an employee on each date, for leave day counting (LV-3). Without a shift no date
  is a weekly off, and a working day declared in the holiday calendar of the employee's
  location on that date is not one either.
- `AttendanceEmails` renders the attendance correction email at send time.
- `summary_rows`, `day_rows` and their headers give the export job the attendance rows.
"""

import uuid
from datetime import date

from app.modules.attendance.days import dates
from app.modules.attendance.emails import AttendanceEmails
from app.modules.attendance.exports import DAYS_HEADER, SUMMARY_HEADER, day_rows, summary_rows
from app.modules.attendance.schedule import Schedule, shift_times
from app.modules.leave import public as leave
from app.platform.db import Session


async def weekly_off_dates(session: Session, employee_id: uuid.UUID, start: date, end: date) -> set[date]:
    schedule = await Schedule.load(session, [employee_id], start, end)
    offs = set()
    for day in dates(start, end):
        found = schedule.shift_on(employee_id, day)
        if found is not None and shift_times(found[1]).is_weekly_off(day):
            offs.add(day)
    located = {day: schedule.location_on(employee_id, day) for day in offs}
    worked = await leave.working_day_dates(
        session, {location for location in located.values() if location is not None}, start, end
    )
    return {
        day for day, location in located.items() if location is None or day not in worked.get(location, set())
    }


__all__ = [
    "DAYS_HEADER",
    "SUMMARY_HEADER",
    "AttendanceEmails",
    "day_rows",
    "summary_rows",
    "weekly_off_dates",
]
