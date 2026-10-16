"""Which shift and attendance policy apply to an employee on a date (docs/database-design.md
§4.5, product-requirements ATT-9, ATT-12).

- Shift: the employee's own assignment on the date, else the default shift of the location
  their job places them in on that date. The shift version effective on the date gives the
  times and weekly offs, so a past day is always judged with the values of that day.
- Policy: the most specific policy effective on the date: one for the shift, else one for
  the location, else the organization's.
- With no shift or no policy the day is not judged: the setup screen lists the gap instead of
  applying a hidden default.

`Schedule.load` reads everything a date range needs for a set of employees in a few queries;
the resolution itself runs in memory.
"""

import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance.derive import PolicyValues, ShiftTimes, WeeklyOff
from app.modules.attendance.models import (
    AttendancePolicy,
    LocationDefaultShift,
    PolicyScope,
    Shift,
    ShiftAssignment,
    ShiftVersion,
)
from app.modules.people import public as people


def _covers(row: Any, on: date) -> bool:
    return bool(row.effective_from <= on and (row.effective_to is None or on < row.effective_to))


def _overlapping(model: Any, start: date, end: date) -> Any:
    return (model.effective_from <= end, or_(model.effective_to.is_(None), model.effective_to > start))


def weekly_offs_of(raw: Iterable[dict[str, Any]]) -> tuple[WeeklyOff, ...]:
    return tuple(
        WeeklyOff(int(item["weekday"]), frozenset(item["weeks"]) if item.get("weeks") else None)
        for item in raw
    )


def shift_times(version: ShiftVersion) -> ShiftTimes:
    return ShiftTimes(
        version.start_time, version.end_time, version.crosses_midnight, weekly_offs_of(version.weekly_offs)
    )


def policy_values(policy: AttendancePolicy) -> PolicyValues:
    return PolicyValues(
        grace_minutes=policy.grace_minutes,
        early_departure_grace_minutes=policy.early_departure_grace_minutes,
        duration_basis=policy.duration_basis,
        full_day_min_minutes=policy.full_day_min_minutes,
        half_day_min_minutes=policy.half_day_min_minutes,
        max_break_minutes=policy.max_break_minutes,
        breaks_count_as_work=policy.breaks_count_as_work,
        break_excess_action=policy.break_excess_action,
        overtime_enabled=policy.overtime_enabled,
        overtime_basis=policy.overtime_basis,
        overtime_min_minutes=policy.overtime_min_minutes,
        overtime_requires_approval=policy.overtime_requires_approval,
        incomplete_after_minutes=policy.incomplete_after_minutes,
        allow_multiple_sessions=policy.allow_multiple_sessions,
    )


@dataclass(frozen=True, slots=True)
class Applied:
    """The shift version and policy that apply to one employee on one date."""

    shift: Shift
    version: ShiftVersion
    policy: AttendancePolicy | None


class Schedule:
    def __init__(
        self,
        *,
        employments: dict[uuid.UUID, people.Employment],
        shifts: dict[uuid.UUID, Shift],
        versions: dict[uuid.UUID, list[ShiftVersion]],
        assignments: dict[uuid.UUID, list[ShiftAssignment]],
        defaults: dict[uuid.UUID, list[LocationDefaultShift]],
        policies: list[AttendancePolicy],
    ) -> None:
        self.employments = employments
        self._shifts = shifts
        self._versions = versions
        self._assignments = assignments
        self._defaults = defaults
        self._policies = policies

    @classmethod
    async def load(
        cls, session: AsyncSession, employee_ids: Collection[uuid.UUID], start: date, end: date
    ) -> "Schedule":
        employments = await people.job_periods(session, employee_ids, start, end)
        location_ids = {job.location_id for e in employments.values() for job in e.jobs}
        assignments: dict[uuid.UUID, list[ShiftAssignment]] = defaultdict(list)
        if employee_ids:
            for row in await session.scalars(
                select(ShiftAssignment).where(
                    ShiftAssignment.employee_id.in_(set(employee_ids)),
                    *_overlapping(ShiftAssignment, start, end),
                )
            ):
                assignments[row.employee_id].append(row)
        defaults: dict[uuid.UUID, list[LocationDefaultShift]] = defaultdict(list)
        if location_ids:
            for default in await session.scalars(
                select(LocationDefaultShift).where(
                    LocationDefaultShift.location_id.in_(location_ids),
                    *_overlapping(LocationDefaultShift, start, end),
                )
            ):
                defaults[default.location_id].append(default)
        shift_ids = {a.shift_id for rows in assignments.values() for a in rows} | {
            d.shift_id for rows in defaults.values() for d in rows
        }
        versions: dict[uuid.UUID, list[ShiftVersion]] = defaultdict(list)
        shifts: dict[uuid.UUID, Shift] = {}
        if shift_ids:
            shifts = {s.id: s for s in await session.scalars(select(Shift).where(Shift.id.in_(shift_ids)))}
            for version in await session.scalars(
                select(ShiftVersion).where(
                    ShiftVersion.shift_id.in_(shift_ids), *_overlapping(ShiftVersion, start, end)
                )
            ):
                versions[version.shift_id].append(version)
        policies = list(
            await session.scalars(select(AttendancePolicy).where(*_overlapping(AttendancePolicy, start, end)))
        )
        return cls(
            employments=employments,
            shifts=shifts,
            versions=versions,
            assignments=assignments,
            defaults=defaults,
            policies=policies,
        )

    def location_on(self, employee_id: uuid.UUID, on: date) -> uuid.UUID | None:
        employment = self.employments.get(employee_id)
        job = employment.job_on(on) if employment else None
        return job.location_id if job else None

    def shift_on(self, employee_id: uuid.UUID, on: date) -> tuple[Shift, ShiftVersion] | None:
        shift_id = next((a.shift_id for a in self._assignments.get(employee_id, ()) if _covers(a, on)), None)
        if shift_id is None:
            location_id = self.location_on(employee_id, on)
            shift_id = (
                next((d.shift_id for d in self._defaults.get(location_id, ()) if _covers(d, on)), None)
                if location_id
                else None
            )
        if shift_id is None:
            return None
        version = next((v for v in self._versions.get(shift_id, ()) if _covers(v, on)), None)
        return (self._shifts[shift_id], version) if version else None

    def policy_on(
        self, shift_id: uuid.UUID | None, location_id: uuid.UUID | None, on: date
    ) -> AttendancePolicy | None:
        candidates = [p for p in self._policies if _covers(p, on)]
        for scope, scope_id in (
            (PolicyScope.SHIFT, shift_id),
            (PolicyScope.LOCATION, location_id),
            (PolicyScope.ORGANIZATION, None),
        ):
            if scope is not PolicyScope.ORGANIZATION and scope_id is None:
                continue
            found = next((p for p in candidates if p.scope_type == scope and p.scope_id == scope_id), None)
            if found is not None:
                return found
        return None

    def applied(self, employee_id: uuid.UUID, on: date) -> Applied | None:
        found = self.shift_on(employee_id, on)
        if found is None:
            return None
        shift, version = found
        return Applied(shift, version, self.policy_on(shift.id, self.location_on(employee_id, on), on))
