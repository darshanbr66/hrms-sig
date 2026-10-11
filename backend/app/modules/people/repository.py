"""Reporting-line queries over effective-dated job history.

A job row applies on a date when `effective_from <= date < effective_to` (or the row is
open-ended); an employee counts on a date when they are employed on it (joined, and not
past their exit date). Reporting lines are resolved as of a date rather than from open-ended
rows, so a future-dated transfer does not move team access before it takes effect.

The recursive queries use UNION, not UNION ALL, so a reporting-line cycle (which the
M2 write path prevents) ends the recursion instead of looping.

The reporting-line functions compute the `team` scope itself, so unlike list queries over
employee records they take no scope filter.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    Text,
    and_,
    exists,
    func,
    literal,
    literal_column,
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.people.models import SEARCH_TEXT, Employee, EmployeeJob, EmployeeStatusChange
from app.platform.authz.engine import Placement


def _job_applies_on(job: type[EmployeeJob], on: date) -> ColumnElement[bool]:
    return and_(job.effective_from <= on, or_(job.effective_to.is_(None), job.effective_to > on))


def _employed_on(employee: type[Employee], on: date) -> ColumnElement[bool]:
    return and_(
        employee.date_of_joining <= on,
        or_(employee.date_of_exit.is_(None), employee.date_of_exit >= on),
    )


def team_member_ids_query(manager_employee_id: uuid.UUID, on: date) -> Select[uuid.UUID]:
    """Employee IDs in the reporting subtree (direct and indirect reports) on a date."""
    job, employee = aliased(EmployeeJob), aliased(Employee)
    team = (
        select(job.employee_id.label("employee_id"))
        .join(employee, employee.id == job.employee_id)
        .where(
            job.manager_employee_id == manager_employee_id,
            _job_applies_on(job, on),
            _employed_on(employee, on),
        )
        .cte("team", recursive=True)
    )
    next_job, next_employee = aliased(EmployeeJob), aliased(Employee)
    team = team.union(
        select(next_job.employee_id)
        .join(next_employee, next_employee.id == next_job.employee_id)
        .join(team, next_job.manager_employee_id == team.c.employee_id)
        .where(_job_applies_on(next_job, on), _employed_on(next_employee, on))
    )
    return select(team.c.employee_id).where(team.c.employee_id != manager_employee_id)


def _reporting_chain_contains_query(
    employee_id: uuid.UUID, manager_employee_id: uuid.UUID, on: date
) -> Select[bool]:
    job, employee = aliased(EmployeeJob), aliased(Employee)
    chain = (
        select(job.manager_employee_id.label("manager_id"))
        .join(employee, employee.id == job.employee_id)
        .where(
            job.employee_id == employee_id,
            job.manager_employee_id.is_not(None),
            _job_applies_on(job, on),
            _employed_on(employee, on),
        )
        .cte("chain", recursive=True)
    )
    next_job, next_employee = aliased(EmployeeJob), aliased(Employee)
    chain = chain.union(
        select(next_job.manager_employee_id)
        .join(next_employee, next_employee.id == next_job.employee_id)
        .join(chain, next_job.employee_id == chain.c.manager_id)
        .where(
            next_job.manager_employee_id.is_not(None),
            _job_applies_on(next_job, on),
            _employed_on(next_employee, on),
        )
    )
    return select(exists().where(chain.c.manager_id == manager_employee_id))


async def is_team_member(
    session: AsyncSession, manager_employee_id: uuid.UUID, employee_id: uuid.UUID, on: date
) -> bool:
    if employee_id == manager_employee_id:
        return False
    query = _reporting_chain_contains_query(employee_id, manager_employee_id, on)
    return bool((await session.execute(query)).scalar_one())


async def team_member_ids(
    session: AsyncSession, manager_employee_id: uuid.UUID, on: date
) -> frozenset[uuid.UUID]:
    return frozenset(await session.scalars(team_member_ids_query(manager_employee_id, on)))


async def has_direct_reports(session: AsyncSession, employee_id: uuid.UUID, on: date) -> bool:
    job, employee = aliased(EmployeeJob), aliased(Employee)
    query = select(
        exists()
        .where(
            job.manager_employee_id == employee_id,
            job.employee_id != employee_id,
            _job_applies_on(job, on),
            _employed_on(employee, on),
        )
        .where(employee.id == job.employee_id)
    )
    return bool((await session.execute(query)).scalar_one())


async def is_employed(session: AsyncSession, employee_id: uuid.UUID, on: date) -> bool:
    employee = aliased(Employee)
    query = select(exists().where(employee.id == employee_id, _employed_on(employee, on)))
    return bool((await session.execute(query)).scalar_one())


async def placement(session: AsyncSession, employee_id: uuid.UUID, on: date) -> Placement | None:
    """The department and location of the job row that applies on a date, for an employee
    employed on it."""
    job, employee = aliased(EmployeeJob), aliased(Employee)
    query = (
        select(job.department_id, job.location_id)
        .join(employee, employee.id == job.employee_id)
        .where(job.employee_id == employee_id, _job_applies_on(job, on), _employed_on(employee, on))
    )
    row = (await session.execute(query)).one_or_none()
    return Placement(row.department_id, row.location_id) if row else None


def placed_employee_ids_query(
    department_id: uuid.UUID | None, location_id: uuid.UUID | None, on: date
) -> Select[uuid.UUID]:
    """Employee IDs placed in a department and/or location on a date (an `all` scope
    restricted by a role assignment, docs/authorization-model.md §5)."""
    job, employee = aliased(EmployeeJob), aliased(Employee)
    query = (
        select(job.employee_id)
        .join(employee, employee.id == job.employee_id)
        .where(_job_applies_on(job, on), _employed_on(employee, on))
    )
    if department_id is not None:
        query = query.where(job.department_id == department_id)
    if location_id is not None:
        query = query.where(job.location_id == location_id)
    return query


async def placed_in_unit_count(session: AsyncSession, unit_column: str, unit_id: uuid.UUID, on: date) -> int:
    """How many employees not yet past their exit date have a current or future job row in a
    department, designation or location (`unit_column` names the job column). Archiving a unit
    with any is refused: they would be left on a unit nobody can choose."""
    job, employee = aliased(EmployeeJob), aliased(Employee)
    column = {
        "department_id": job.department_id,
        "designation_id": job.designation_id,
        "location_id": job.location_id,
    }[unit_column]
    query = (
        select(func.count(func.distinct(job.employee_id)))
        .join(employee, employee.id == job.employee_id)
        .where(
            column == unit_id,
            or_(job.effective_to.is_(None), job.effective_to > on),
            or_(employee.date_of_exit.is_(None), employee.date_of_exit >= on),
        )
    )
    return int((await session.execute(query)).scalar_one())


async def first_name(session: AsyncSession, employee_id: uuid.UUID) -> str | None:
    """Preferred name, else legal first name (`public_internal` directory fields)."""
    query = select(func.coalesce(Employee.preferred_name, Employee.legal_first_name)).where(
        Employee.id == employee_id
    )
    value: str | None = (await session.execute(query)).scalar_one_or_none()
    return value


async def work_email(session: AsyncSession, employee_id: uuid.UUID) -> tuple[bool, str | None]:
    """Whether the employee exists, and their work email (`public_internal`)."""
    row = (await session.execute(select(Employee.work_email).where(Employee.id == employee_id))).one_or_none()
    return (False, None) if row is None else (True, row.work_email)


# --- employee records (M2) ---------------------------------------------------------------------
# List functions take the caller's scope filter (a predicate on the employee ID) as a required
# argument, so no list can run unscoped (docs/authorization-model.md §9).


def display_name() -> ColumnElement[str]:
    """Preferred name (else legal first name), then legal last name."""
    first = func.coalesce(Employee.preferred_name, Employee.legal_first_name)
    return func.trim(first + " " + func.coalesce(Employee.legal_last_name, ""))


def name_of(employee: Employee) -> str:
    """The display name: preferred name (else legal first name), then legal last name."""
    return f"{employee.preferred_name or employee.legal_first_name} {employee.legal_last_name or ''}".strip()


def employed_on(on: date) -> ColumnElement[bool]:
    return _employed_on(Employee, on)


def _search(text_query: str) -> ColumnElement[bool]:
    escaped = text_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return literal_column(f"({SEARCH_TEXT})", Text()).ilike(pattern)


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


def employment_on(employment: str, on: date) -> ColumnElement[bool]:
    """The employment definitions every list and count uses (docs/product-requirements.md
    EMP-7), all on one calendar date (the UTC date, like authorization):

    - current: joined on or before the date, and no last working day before it;
    - future: joining after the date;
    - former: last working day before the date.

    These follow the dates, not the recorded status: someone whose last working day is today
    is still current today although their status is already `exited`, and someone whose
    joining date has passed is current although HR has not yet marked them active."""
    if employment == "future":
        return Employee.date_of_joining > on
    if employment == "former":
        return and_(Employee.date_of_exit.is_not(None), Employee.date_of_exit < on)
    return _employed_on(Employee, on)


def _sort_key(sort: str) -> tuple[ColumnElement[str], bool]:
    """The text sort key for keyset pagination, and whether it runs newest first. ISO dates
    sort correctly as text."""
    if sort == "code":
        return func.lower(Employee.employee_code), False
    if sort in ("joined_newest", "joined_oldest"):
        return func.to_char(Employee.date_of_joining, "YYYY-MM-DD"), sort == "joined_newest"
    return func.lower(display_name()), False


async def list_employees(
    session: AsyncSession,
    scope: ColumnElement[bool],
    filters: EmployeeFilters,
    *,
    on: date,
    after: tuple[str, uuid.UUID] | None,
    limit: int,
) -> list[tuple[Employee, str]]:
    """Employees in scope with their sort key, ordered by the chosen key then ID."""
    sort, descending = _sort_key(filters.sort)
    query = select(Employee, sort.label("sort_key")).where(scope)
    if filters.text:
        query = query.where(_search(filters.text))
    if filters.statuses:
        query = query.where(Employee.status.in_(filters.statuses))
    if filters.employment:
        query = query.where(employment_on(filters.employment, on))
    if filters.department_id or filters.designation_id or filters.location_id or filters.manager_id:
        job = aliased(EmployeeJob)
        query = query.join(job, and_(job.employee_id == Employee.id, _job_applies_on(job, on)))
        if filters.department_id:
            query = query.where(job.department_id == filters.department_id)
        if filters.designation_id:
            query = query.where(job.designation_id == filters.designation_id)
        if filters.location_id:
            query = query.where(job.location_id == filters.location_id)
        if filters.manager_id:
            query = query.where(job.manager_employee_id == filters.manager_id)
    if after is not None:
        key, after_key = tuple_(sort, Employee.id), tuple_(literal(after[0]), literal(after[1]))
        query = query.where(key < after_key if descending else key > after_key)
    order = (sort.desc(), Employee.id.desc()) if descending else (sort, Employee.id)
    rows = (await session.execute(query.order_by(*order).limit(limit + 1))).all()
    return [(row[0], row[1]) for row in rows]


async def current_jobs(
    session: AsyncSession, employee_ids: Collection[uuid.UUID], on: date
) -> dict[uuid.UUID, EmployeeJob]:
    """The job row that applies on a date, per employee (none before the first row)."""
    if not employee_ids:
        return {}
    query = select(EmployeeJob).where(
        EmployeeJob.employee_id.in_(set(employee_ids)), _job_applies_on(EmployeeJob, on)
    )
    return {job.employee_id: job for job in await session.scalars(query)}


async def latest_jobs(
    session: AsyncSession, employee_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, EmployeeJob]:
    """Each employee's most recent job row, for those with no row applying today (not yet joined,
    or exited)."""
    if not employee_ids:
        return {}
    query = (
        select(EmployeeJob)
        .where(EmployeeJob.employee_id.in_(set(employee_ids)))
        .order_by(EmployeeJob.employee_id, EmployeeJob.effective_from.desc())
        .ext(distinct_on(EmployeeJob.employee_id))
    )
    return {job.employee_id: job for job in await session.scalars(query)}


async def job_history(session: AsyncSession, employee_id: uuid.UUID) -> list[EmployeeJob]:
    query = (
        select(EmployeeJob)
        .where(EmployeeJob.employee_id == employee_id)
        .order_by(EmployeeJob.effective_from.desc())
    )
    return list(await session.scalars(query))


async def employee_names(session: AsyncSession, employee_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not employee_ids:
        return {}
    rows = (
        await session.execute(select(Employee.id, display_name()).where(Employee.id.in_(set(employee_ids))))
    ).all()
    return {row[0]: row[1] for row in rows}


async def directory(
    session: AsyncSession,
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


async def statistics_rows(session: AsyncSession, scope: ColumnElement[bool]) -> list[Any]:
    query = select(
        Employee.id,
        display_name().label("name"),
        Employee.status,
        Employee.date_of_joining,
        Employee.date_of_exit,
    ).where(scope)
    return list((await session.execute(query)).all())


async def headcounts(
    session: AsyncSession, scope: ColumnElement[bool], on: date, unit_column: str
) -> list[tuple[uuid.UUID, int]]:
    """Employees in scope employed on the date, counted by the department, designation or
    location (`unit_column`) of the job row that applies on it."""
    job = aliased(EmployeeJob)
    column = {
        "department_id": job.department_id,
        "designation_id": job.designation_id,
        "location_id": job.location_id,
    }[unit_column]
    query = (
        select(column, func.count())
        .select_from(Employee)
        .join(job, and_(job.employee_id == Employee.id, _job_applies_on(job, on)))
        .where(scope, _employed_on(Employee, on))
        .group_by(column)
    )
    return [(row[0], int(row[1])) for row in (await session.execute(query)).all()]


async def status_history(session: AsyncSession, employee_id: uuid.UUID) -> list[EmployeeStatusChange]:
    query = (
        select(EmployeeStatusChange)
        .where(EmployeeStatusChange.employee_id == employee_id)
        .order_by(EmployeeStatusChange.created_at.desc(), EmployeeStatusChange.id.desc())
    )
    return list(await session.scalars(query))


async def exit_date(session: AsyncSession, employee_id: uuid.UUID) -> tuple[bool, date | None]:
    """Whether the employee exists, and their last working day if they have left or are leaving."""
    row = (
        await session.execute(select(Employee.date_of_exit).where(Employee.id == employee_id))
    ).one_or_none()
    return (False, None) if row is None else (True, row.date_of_exit)


async def employee_exists(session: AsyncSession, employee_id: uuid.UUID) -> bool:
    return bool((await session.execute(select(exists().where(Employee.id == employee_id)))).scalar_one())
