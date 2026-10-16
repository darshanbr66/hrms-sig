"""Attendance rows for exports (product-requirements RPT-1 to RPT-3). The employees are the
ones the requester could read when the export was requested; the export job passes them in."""

import uuid
from collections.abc import Collection
from datetime import date, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance.days import judge
from app.modules.attendance.service import totals_for
from app.modules.people import public as people
from app.platform.csvfile import Cell

STATUSES: Final = (
    "present",
    "half_day",
    "absent",
    "on_leave",
    "holiday",
    "weekly_off",
    "incomplete",
    "pending_correction",
)
SUMMARY_HEADER: Final = (
    "employee_code",
    "name",
    "department",
    "location",
    *(f"days_{status}" for status in STATUSES),
    "days_not_judged",
    "effective_minutes",
    "late_days",
    "late_minutes",
    "early_leave_days",
    "overtime_minutes",
)
DAYS_HEADER: Final = (
    "employee_code",
    "name",
    "work_date",
    "status",
    "shift",
    "first_in",
    "last_out",
    "worked_minutes",
    "break_minutes",
    "effective_minutes",
    "late_minutes",
    "early_leave_minutes",
    "overtime_minutes",
    "anomalies",
    "time_zone",
)


async def _colleagues(
    session: AsyncSession, employee_ids: Collection[uuid.UUID], start: date, end: date
) -> list[people.Colleague]:
    found, _ = await people.employed_colleagues(
        session, people.employee_id_column().in_(list(employee_ids)), on=end, since=start
    )
    return found


async def summary_rows(
    session: AsyncSession, employee_ids: Collection[uuid.UUID], start: date, end: date, now: datetime
) -> list[list[Cell]]:
    colleagues = await _colleagues(session, employee_ids, start, end)
    judged = await judge(session, [c.id for c in colleagues], start, end, now)
    rows: list[list[Cell]] = []
    for colleague in colleagues:
        totals = totals_for(judged.get(colleague.id, []), colleague)
        rows.append(
            [
                colleague.employee_code,
                colleague.name,
                colleague.department_name,
                colleague.location_name,
                *(totals.days.get(status, 0) for status in STATUSES),
                totals.not_judged,
                totals.effective_minutes,
                totals.late_days,
                totals.late_minutes,
                totals.early_leave_days,
                totals.overtime_minutes,
            ]
        )
    return rows


async def day_rows(
    session: AsyncSession, employee_ids: Collection[uuid.UUID], start: date, end: date, now: datetime
) -> list[list[Cell]]:
    colleagues = await _colleagues(session, employee_ids, start, end)
    judged = await judge(session, [c.id for c in colleagues], start, end, now)
    rows: list[list[Cell]] = []
    for colleague in colleagues:
        for day in judged.get(colleague.id, []):
            if not day.notable:
                continue
            summary = day.summary
            rows.append(
                [
                    colleague.employee_code,
                    colleague.name,
                    day.work_date,
                    summary.status.value if summary.status else "not_judged",
                    day.shift_name,
                    summary.first_in,
                    summary.last_out,
                    summary.gross_minutes,
                    summary.break_minutes,
                    summary.effective_minutes,
                    summary.late_minutes,
                    summary.early_leave_minutes,
                    summary.overtime_minutes,
                    " ".join(summary.anomalies),
                    day.time_zone,
                ]
            )
    return rows
