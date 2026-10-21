"""Reporting-line queries over effective-dated job history, and employee lists.

A job row applies on a date when `effective_from <= date < effective_to` (or the row is
open-ended); an employee counts on a date when they are employed on it (joined, and not past
their exit date). Reporting lines are resolved as of a date rather than from open-ended rows,
so a future-dated transfer does not move team access before it takes effect.

Reporting lines are walked in memory from the job rows that apply on the date (one query),
restricted to people employed on it. A reporting-line cycle (which the write path prevents)
ends the walk instead of looping. Every query runs in the session's organization.

The reporting-line functions compute the `team` scope itself, so unlike list queries over
employee records they take no scope filter.
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.modules.people.models import Employee, EmployeeJob, EmployeeStatusChange, display_name_of
from app.platform.authz.engine import Placement
from app.platform.db import Session


def job_applies_on(on: date) -> dict[str, Any]:
    return {
        "$and": [
            {"effective_from": {"$lte": on}},
            {"$or": [{"effective_to": None}, {"effective_to": {"$gt": on}}]},
        ]
    }


def employed_on(on: date) -> dict[str, Any]:
    return {
        "$and": [
            {"date_of_joining": {"$lte": on}},
            {"$or": [{"date_of_exit": None}, {"date_of_exit": {"$gte": on}}]},
        ]
    }


def _all(*conditions: Mapping[str, Any]) -> dict[str, Any]:
    present = [dict(condition) for condition in conditions if condition]
    if not present:
        return {}
    return present[0] if len(present) == 1 else {"$and": present}


async def _employed_ids(session: Session, on: date) -> set[uuid.UUID]:
    return set(await session.distinct(Employee, "id", employed_on(on)))


async def _reports_by_manager(session: Session, on: date) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Each manager's direct reports on a date, among people employed on it."""
    employed = await _employed_ids(session, on)
    jobs = await session.find(EmployeeJob, _all(job_applies_on(on), {"manager_employee_id": {"$ne": None}}))
    reports: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for job in jobs:
        if job.manager_employee_id is not None and job.employee_id in employed:
            reports[job.manager_employee_id].add(job.employee_id)
    return reports


async def team_member_ids(session: Session, manager_employee_id: uuid.UUID, on: date) -> frozenset[uuid.UUID]:
    """Employee IDs in the reporting subtree (direct and indirect reports) on a date."""
    reports = await _reports_by_manager(session, on)
    found: set[uuid.UUID] = set()
    pending = [manager_employee_id]
    while pending:
        for member in reports.get(pending.pop(), ()):
            if member not in found and member != manager_employee_id:
                found.add(member)
                pending.append(member)
    return frozenset(found)


async def is_team_member(
    session: Session, manager_employee_id: uuid.UUID, employee_id: uuid.UUID, on: date
) -> bool:
    """Whether `manager_employee_id` is above `employee_id` in the reporting chain on a date."""
    if employee_id == manager_employee_id:
        return False
    employed = await _employed_ids(session, on)
    seen: set[uuid.UUID] = set()
    current = employee_id
    while current not in seen:
        seen.add(current)
        if current not in employed:
            return False
        job = await session.find_one(EmployeeJob, _all(job_applies_on(on), {"employee_id": current}))
        if job is None or job.manager_employee_id is None:
            return False
        if job.manager_employee_id == manager_employee_id:
            return True
        current = job.manager_employee_id
    return False


async def has_direct_reports(session: Session, employee_id: uuid.UUID, on: date) -> bool:
    jobs = await session.find(
        EmployeeJob,
        _all(job_applies_on(on), {"manager_employee_id": employee_id, "employee_id": {"$ne": employee_id}}),
    )
    if not jobs:
        return False
    return await session.exists(
        Employee, _all(employed_on(on), {"id": {"$in": [job.employee_id for job in jobs]}})
    )


async def is_employed(session: Session, employee_id: uuid.UUID, on: date) -> bool:
    return await session.exists(Employee, _all(employed_on(on), {"id": employee_id}))


async def placement(session: Session, employee_id: uuid.UUID, on: date) -> Placement | None:
    """The department and location of the job row that applies on a date, for an employee
    employed on it."""
    if not await is_employed(session, employee_id, on):
        return None
    job = await session.find_one(EmployeeJob, _all(job_applies_on(on), {"employee_id": employee_id}))
    return Placement(job.department_id, job.location_id) if job else None


async def placed_employee_ids(
    session: Session, department_id: uuid.UUID | None, location_id: uuid.UUID | None, on: date
) -> frozenset[uuid.UUID]:
    """Employee IDs placed in a department and/or location on a date (an `all` scope
    restricted by a role assignment, docs/authorization-model.md §5)."""
    unit: dict[str, Any] = {}
    if department_id is not None:
        unit["department_id"] = department_id
    if location_id is not None:
        unit["location_id"] = location_id
    placed = set(await session.distinct(EmployeeJob, "employee_id", _all(job_applies_on(on), unit)))
    return frozenset(placed & await _employed_ids(session, on))


async def placed_in_unit_count(session: Session, unit_column: str, unit_id: uuid.UUID, on: date) -> int:
    """How many employees not yet past their exit date have a current or future job row in a
    department, designation or location (`unit_column` names the job field). Archiving a unit
    with any is refused: they would be left on a unit nobody can choose."""
    if unit_column not in ("department_id", "designation_id", "location_id"):
        raise ValueError(f"unknown unit field {unit_column}")
    placed = await session.distinct(
        EmployeeJob,
        "employee_id",
        {unit_column: unit_id, "$or": [{"effective_to": None}, {"effective_to": {"$gt": on}}]},
    )
    if not placed:
        return 0
    return await session.count(
        Employee, {"id": {"$in": placed}, "$or": [{"date_of_exit": None}, {"date_of_exit": {"$gte": on}}]}
    )


async def first_name(session: Session, employee_id: uuid.UUID) -> str | None:
    """Preferred name, else legal first name (`public_internal` directory fields)."""
    employee = await session.get(Employee, employee_id)
    return None if employee is None else employee.preferred_name or employee.legal_first_name


async def work_email(session: Session, employee_id: uuid.UUID) -> tuple[bool, str | None]:
    """Whether the employee exists, and their work email (`public_internal`)."""
    employee = await session.get(Employee, employee_id)
    return (False, None) if employee is None else (True, employee.work_email)


# --- employee records --------------------------------------------------------------------------
# List functions take the caller's scope filter (a condition on the employee ID) as a required
# argument, so no list can run unscoped (docs/authorization-model.md §9).


def name_of(employee: Employee) -> str:
    """The display name: preferred name (else legal first name), then legal last name."""
    return display_name_of(employee.preferred_name, employee.legal_first_name, employee.legal_last_name)


def _search(text_query: str) -> dict[str, Any]:
    return {"search_text": {"$regex": re.escape(text_query.lower())}}


@dataclass(frozen=True, slots=True)
class EmployeeFilters:
    text: str | None = None
    statuses: tuple[str, ...] = ()
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    # current | future | former (dates on the day, see `employment_on`); None for everyone.
    employment: str | None = None
    # name | code | joined_newest | joined_oldest
    sort: str = "name"


def employment_on(employment: str, on: date) -> dict[str, Any]:
    """The employment definitions every list and count uses (docs/product-requirements.md
    EMP-7), all on one calendar date (the UTC date, like authorization):

    - current: joined on or before the date, and no last working day before it;
    - future: joining after the date;
    - former: last working day before the date.

    These follow the dates, not the recorded status: someone whose last working day is today
    is still current today although their status is already `exited`, and someone whose
    joining date has passed is current although HR has not yet marked them active."""
    if employment == "future":
        return {"date_of_joining": {"$gt": on}}
    if employment == "former":
        return {"$and": [{"date_of_exit": {"$ne": None}}, {"date_of_exit": {"$lt": on}}]}
    return employed_on(on)


def _sort_field(sort: str) -> tuple[str, bool]:
    """The stored sort key for keyset pagination, and whether it runs newest first. ISO dates
    sort correctly as text."""
    if sort == "code":
        return "code_key", False
    if sort in ("joined_newest", "joined_oldest"):
        return "date_of_joining", sort == "joined_newest"
    return "sort_name", False


def _sort_value(employee: Employee, field: str) -> str:
    value = getattr(employee, field)
    return value.isoformat() if isinstance(value, date) else str(value)


async def list_employees(
    session: Session,
    scope: Mapping[str, Any],
    filters: EmployeeFilters,
    *,
    on: date,
    after: tuple[str, uuid.UUID] | None,
    limit: int,
) -> list[tuple[Employee, str]]:
    """Employees in scope with their sort key, ordered by the chosen key then ID."""
    field, descending = _sort_field(filters.sort)
    conditions: list[Mapping[str, Any]] = [scope]
    if filters.text:
        conditions.append(_search(filters.text))
    if filters.statuses:
        conditions.append({"status": {"$in": list(filters.statuses)}})
    if filters.employment:
        conditions.append(employment_on(filters.employment, on))
    if filters.department_id or filters.designation_id or filters.location_id or filters.manager_id:
        unit: dict[str, Any] = {}
        if filters.department_id:
            unit["department_id"] = filters.department_id
        if filters.designation_id:
            unit["designation_id"] = filters.designation_id
        if filters.location_id:
            unit["location_id"] = filters.location_id
        if filters.manager_id:
            unit["manager_employee_id"] = filters.manager_id
        placed = await session.distinct(EmployeeJob, "employee_id", _all(job_applies_on(on), unit))
        conditions.append({"id": {"$in": placed}})
    if after is not None:
        key: Any = date.fromisoformat(after[0]) if field == "date_of_joining" else after[0]
        beyond = "$lt" if descending else "$gt"
        conditions.append({"$or": [{field: {beyond: key}}, {field: key, "id": {beyond: after[1]}}]})
    direction = -1 if descending else 1
    employees = await session.find(
        Employee, _all(*conditions), sort=[(field, direction), ("id", direction)], limit=limit + 1
    )
    return [(employee, _sort_value(employee, field)) for employee in employees]


async def current_jobs(
    session: Session, employee_ids: Collection[uuid.UUID], on: date
) -> dict[uuid.UUID, EmployeeJob]:
    """The job row that applies on a date, per employee (none before the first row)."""
    if not employee_ids:
        return {}
    jobs = await session.find(
        EmployeeJob, _all(job_applies_on(on), {"employee_id": {"$in": list(set(employee_ids))}})
    )
    return {job.employee_id: job for job in jobs}


async def latest_jobs(session: Session, employee_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, EmployeeJob]:
    """Each employee's most recent job row, for those with no row applying today (not yet joined,
    or exited)."""
    if not employee_ids:
        return {}
    jobs = await session.find(
        EmployeeJob, {"employee_id": {"$in": list(set(employee_ids))}}, sort=[("effective_from", -1)]
    )
    latest: dict[uuid.UUID, EmployeeJob] = {}
    for job in jobs:
        latest.setdefault(job.employee_id, job)
    return latest


async def job_history(session: Session, employee_id: uuid.UUID) -> list[EmployeeJob]:
    return await session.find(EmployeeJob, {"employee_id": employee_id}, sort=[("effective_from", -1)])


async def employee_names(session: Session, employee_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not employee_ids:
        return {}
    employees = await session.find(Employee, {"id": {"$in": list(set(employee_ids))}})
    return {employee.id: name_of(employee) for employee in employees}


async def directory(
    session: Session,
    filters: EmployeeFilters,
    *,
    on: date,
    after: tuple[str, uuid.UUID] | None,
    limit: int,
) -> list[tuple[Employee, str]]:
    """Employees employed on the date and not exited: the directory (product-requirements EMP-6)
    is open to every employee, so it needs no scope filter beyond this."""
    directory_filters = EmployeeFilters(
        text=filters.text,
        statuses=("active", "on_notice"),
        department_id=filters.department_id,
        location_id=filters.location_id,
    )
    return await list_employees(session, employed_on(on), directory_filters, on=on, after=after, limit=limit)


@dataclass(frozen=True, slots=True)
class StatisticsRow:
    id: uuid.UUID
    name: str
    status: str
    date_of_joining: date
    date_of_exit: date | None


async def statistics_rows(session: Session, scope: Mapping[str, Any]) -> list[StatisticsRow]:
    return [
        StatisticsRow(
            employee.id, name_of(employee), employee.status, employee.date_of_joining, employee.date_of_exit
        )
        for employee in await session.find(Employee, scope)
    ]


async def headcounts(
    session: Session, scope: Mapping[str, Any], on: date, unit_column: str
) -> list[tuple[uuid.UUID, int]]:
    """Employees in scope employed on the date, counted by the department, designation or
    location (`unit_column`) of the job row that applies on it."""
    if unit_column not in ("department_id", "designation_id", "location_id"):
        raise ValueError(f"unknown unit field {unit_column}")
    employed = set(await session.distinct(Employee, "id", _all(scope, employed_on(on))))
    counts: dict[uuid.UUID, int] = defaultdict(int)
    for job in await session.find(EmployeeJob, job_applies_on(on)):
        if job.employee_id in employed:
            counts[getattr(job, unit_column)] += 1
    return list(counts.items())


async def status_history(session: Session, employee_id: uuid.UUID) -> list[EmployeeStatusChange]:
    return await session.find(
        EmployeeStatusChange, {"employee_id": employee_id}, sort=[("created_at", -1), ("id", -1)]
    )


async def exit_date(session: Session, employee_id: uuid.UUID) -> tuple[bool, date | None]:
    """Whether the employee exists, and their last working day if they have left or are leaving."""
    employee = await session.get(Employee, employee_id)
    return (False, None) if employee is None else (True, employee.date_of_exit)


async def employee_exists(session: Session, employee_id: uuid.UUID) -> bool:
    return await session.exists(Employee, {"id": employee_id})
