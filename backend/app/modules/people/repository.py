"""Reporting-line queries over effective-dated job history.

A job row applies on a date when `effective_from <= date < effective_to` (or the row is
open-ended); an employee counts on a date when they are employed on it (joined, and not
past their exit date). Reporting lines are resolved as of a date rather than from open-ended
rows, so a future-dated transfer does not move team access before it takes effect.

The recursive queries use UNION, not UNION ALL, so a reporting-line cycle (which the
M2 write path prevents) ends the recursion instead of looping.

These functions compute the `team` scope itself, so unlike list queries over employee
records they take no scope filter.
"""

import uuid
from datetime import date

from sqlalchemy import ColumnElement, Select, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.people.models import Employee, EmployeeJob


def _job_applies_on(job: type[EmployeeJob], on: date) -> ColumnElement[bool]:
    return and_(job.effective_from <= on, or_(job.effective_to.is_(None), job.effective_to > on))


def _employed_on(employee: type[Employee], on: date) -> ColumnElement[bool]:
    return and_(
        employee.date_of_joining <= on,
        or_(employee.date_of_exit.is_(None), employee.date_of_exit >= on),
    )


def team_member_ids_query(manager_employee_id: uuid.UUID, on: date) -> Select[tuple[uuid.UUID]]:
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
) -> Select[tuple[bool]]:
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
