"""The people module's interface for other modules (docs/architecture.md §4).

Reporting lines and placement for the authorization engine (docs/authorization-model.md §5, §7):

- `team_member_ids_query` is a SELECT of employee IDs, for embedding in a list query's scope
  filter as `employee_id IN (...)` without materializing the team first.
- `is_team_member` answers the single-record question by walking up the reporting chain.
- `has_direct_reports` decides the derived `manager` role; `is_employed` the derived
  `employee` role.
- `placement` and `placed_employee_ids_query` apply a role assignment's department or
  location constraint.
- `relationships` bundles these for the engine's `Relationships` protocol.
- `employee_exists` lets other modules check a reference to an employee record;
  `exit_date` also says whether the person has left. `employee_names` gives display names
  (directory fields, visible to every employee).
- `placed_in_unit_count` tells the org module whether anyone is still placed in a unit it
  is about to archive.
- `job_facts` gives the employment dates and the job (location, department, employment type,
  manager) that applies on a date, for modules whose rules depend on them (leave).
- `employed_colleagues` lists employees within a caller's scope filter (built on
  `employee_id_column`) with where they work, for modules that show their own data per person.
- `employee_ids_by_code` finds employees by code, for imports.
- `ids_in_scope` materializes a caller's scope filter (an export fixes who it covers when it
  is requested); `employee_rows` and `EMPLOYEES_HEADER` give the employee export.
- `job_periods` gives each employee's employment dates and the job rows overlapping a date
  range, for modules that judge many days at once (attendance).

Every function takes the calendar date to resolve on. "Now" is the current date where the
caller decides it; a historical record passes the record's own date, which gives the "team
at the time of the record" rule.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from app.modules.org import public as org
from app.modules.people import repository
from app.modules.people.exports import EMPLOYEES_HEADER, employee_rows
from app.modules.people.models import Employee, EmployeeJob
from app.modules.people.repository import (
    employee_exists,
    employee_names,
    exit_date,
    first_name,
    has_direct_reports,
    is_employed,
    is_team_member,
    placed_employee_ids_query,
    placed_in_unit_count,
    placement,
    team_member_ids,
    team_member_ids_query,
    work_email,
)


class PeopleRelationships:
    """Implements `app.platform.authz.engine.Relationships`."""

    is_team_member = staticmethod(is_team_member)
    team_member_ids_query = staticmethod(team_member_ids_query)
    placement = staticmethod(placement)
    placed_employee_ids_query = staticmethod(placed_employee_ids_query)


relationships = PeopleRelationships()

# More employees than the design load (product-requirements §7), so an unlimited listing is
# still bounded.
MAX_COLLEAGUES: Final = 5000

__all__ = [
    "EMPLOYEES_HEADER",
    "MAX_COLLEAGUES",
    "Colleague",
    "PeopleRelationships",
    "employed_colleagues",
    "employee_exists",
    "employee_id_column",
    "employee_ids_by_code",
    "employee_names",
    "employee_rows",
    "exit_date",
    "first_name",
    "has_direct_reports",
    "ids_in_scope",
    "is_employed",
    "is_team_member",
    "job_facts",
    "job_periods",
    "placed_employee_ids_query",
    "placed_in_unit_count",
    "placement",
    "relationships",
    "team_member_ids",
    "team_member_ids_query",
    "work_email",
]


# --- job facts for other modules ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobFacts:
    """An employee's employment dates and the job row that applies on a date (none before the
    first row or after the last one closes)."""

    date_of_joining: date
    date_of_exit: date | None
    location_id: uuid.UUID | None
    department_id: uuid.UUID | None
    employment_type: str | None
    manager_employee_id: uuid.UUID | None

    def employed_on(self, on: date) -> bool:
        return self.date_of_joining <= on and (self.date_of_exit is None or self.date_of_exit >= on)


async def job_facts(session: AsyncSession, employee_id: uuid.UUID, on: date) -> JobFacts | None:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        return None
    job = (await repository.current_jobs(session, [employee_id], on)).get(employee_id)
    return JobFacts(
        date_of_joining=employee.date_of_joining,
        date_of_exit=employee.date_of_exit,
        location_id=job.location_id if job else None,
        department_id=job.department_id if job else None,
        employment_type=job.employment_type if job else None,
        manager_employee_id=job.manager_employee_id if job else None,
    )


@dataclass(frozen=True, slots=True)
class JobPeriod:
    effective_from: date
    # Exclusive; None while open-ended.
    effective_to: date | None
    location_id: uuid.UUID
    department_id: uuid.UUID
    employment_type: str

    def applies_on(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on < self.effective_to)


@dataclass(frozen=True, slots=True)
class Employment:
    date_of_joining: date
    date_of_exit: date | None
    jobs: tuple[JobPeriod, ...]

    def employed_on(self, on: date) -> bool:
        return self.date_of_joining <= on and (self.date_of_exit is None or self.date_of_exit >= on)

    def job_on(self, on: date) -> JobPeriod | None:
        return next((job for job in self.jobs if job.applies_on(on)), None)


async def ids_in_scope(
    session: AsyncSession, scope: ColumnElement[bool], *, limit: int = MAX_COLLEAGUES
) -> list[uuid.UUID]:
    """Every employee ID a scope filter (built on `employee_id_column`) covers, at most `limit`."""
    return list(await session.scalars(select(Employee.id).where(scope).order_by(Employee.id).limit(limit)))


async def employee_ids_by_code(session: AsyncSession, codes: Collection[str]) -> dict[str, uuid.UUID]:
    """Employee IDs by lower-cased employee code, for imports that name people by code."""
    if not codes:
        return {}
    lowered = {code.lower() for code in codes}
    rows = await session.execute(
        select(Employee.id, Employee.employee_code).where(func.lower(Employee.employee_code).in_(lowered))
    )
    return {code.lower(): employee_id for employee_id, code in rows.all()}


async def job_periods(
    session: AsyncSession, employee_ids: Collection[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, Employment]:
    """Employment dates and the job rows overlapping `start`..`end` (inclusive), per employee."""
    if not employee_ids:
        return {}
    ids = set(employee_ids)
    employees = {row.id: row for row in await session.scalars(select(Employee).where(Employee.id.in_(ids)))}
    jobs: dict[uuid.UUID, list[JobPeriod]] = {employee_id: [] for employee_id in employees}
    rows = await session.scalars(
        select(EmployeeJob)
        .where(
            EmployeeJob.employee_id.in_(ids),
            EmployeeJob.effective_from <= end,
            or_(EmployeeJob.effective_to.is_(None), EmployeeJob.effective_to > start),
        )
        .order_by(EmployeeJob.employee_id, EmployeeJob.effective_from)
    )
    for job in rows:
        jobs[job.employee_id].append(
            JobPeriod(
                job.effective_from, job.effective_to, job.location_id, job.department_id, job.employment_type
            )
        )
    return {
        employee_id: Employment(employee.date_of_joining, employee.date_of_exit, tuple(jobs[employee_id]))
        for employee_id, employee in employees.items()
    }


# --- scoped employee listing for other modules -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Colleague:
    """Internal-tier summary of an employee another module lists alongside its own data."""

    id: uuid.UUID
    name: str
    employee_code: str
    department_name: str | None
    designation_name: str | None
    location_id: uuid.UUID | None
    location_name: str | None


def employee_id_column() -> QueryableAttribute[uuid.UUID]:
    """The employee ID column a caller's scope filter applies to, for `employed_colleagues`."""
    return Employee.id


async def employed_colleagues(
    session: AsyncSession,
    scope: ColumnElement[bool],
    *,
    on: date,
    since: date | None = None,
    text: str | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    after: tuple[str, uuid.UUID] | None = None,
    limit: int | None = None,
) -> tuple[list[Colleague], tuple[str, uuid.UUID] | None]:
    """Employees in `scope` who are employed on the date and not exited, by name, with where
    they work. With `since`, everyone employed on any day from `since` to `on` instead, people
    who have since left included (for reports over a period). Without a limit, every match is
    returned (for counts over a scope)."""
    filters = repository.EmployeeFilters(
        text=text,
        statuses=() if since else ("active", "on_notice"),
        department_id=department_id,
        location_id=location_id,
    )
    employed = (
        and_(
            Employee.date_of_joining <= on,
            or_(Employee.date_of_exit.is_(None), Employee.date_of_exit >= since),
        )
        if since
        else repository.employed_on(on)
    )
    rows = await repository.list_employees(
        session,
        and_(scope, employed),
        filters,
        on=on,
        after=after,
        limit=limit if limit is not None else MAX_COLLEAGUES,
    )
    page = rows if limit is None else rows[:limit]
    jobs = await repository.current_jobs(session, [employee.id for employee, _ in page], on)
    if since:
        # People who left before `on` are shown where they last worked.
        jobs |= await repository.latest_jobs(session, [e.id for e, _ in page if e.id not in jobs])
    units = await org.unit_names(
        session,
        department_ids={job.department_id for job in jobs.values()},
        designation_ids={job.designation_id for job in jobs.values()},
        location_ids={job.location_id for job in jobs.values()},
    )
    colleagues = []
    for employee, _ in page:
        job = jobs.get(employee.id)
        colleagues.append(
            Colleague(
                id=employee.id,
                name=repository.name_of(employee),
                employee_code=employee.employee_code,
                department_name=units.departments.get(job.department_id) if job else None,
                designation_name=units.designations.get(job.designation_id) if job else None,
                location_id=job.location_id if job else None,
                location_name=units.locations.get(job.location_id) if job else None,
            )
        )
    next_after = (page[-1][1], page[-1][0].id) if limit is not None and len(rows) > limit else None
    return colleagues, next_after
