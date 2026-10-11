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

Every function takes the calendar date to resolve on. "Now" is the current date where the
caller decides it; a historical record passes the record's own date, which gives the "team
at the time of the record" rule.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import ColumnElement, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from app.modules.org import public as org
from app.modules.people import repository
from app.modules.people.models import Employee
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
    "MAX_COLLEAGUES",
    "Colleague",
    "PeopleRelationships",
    "employed_colleagues",
    "employee_exists",
    "employee_id_column",
    "employee_names",
    "exit_date",
    "first_name",
    "has_direct_reports",
    "is_employed",
    "is_team_member",
    "job_facts",
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
    text: str | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    after: tuple[str, uuid.UUID] | None = None,
    limit: int | None = None,
) -> tuple[list[Colleague], tuple[str, uuid.UUID] | None]:
    """Employees in `scope` who are employed on the date and not exited, by name, with where
    they work. Without a limit, every match is returned (for counts over a scope)."""
    filters = repository.EmployeeFilters(
        text=text, statuses=("active", "on_notice"), department_id=department_id, location_id=location_id
    )
    rows = await repository.list_employees(
        session,
        and_(scope, repository.employed_on(on)),
        filters,
        on=on,
        after=after,
        limit=limit if limit is not None else MAX_COLLEAGUES,
    )
    page = rows if limit is None else rows[:limit]
    jobs = await repository.current_jobs(session, [employee.id for employee, _ in page], on)
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
