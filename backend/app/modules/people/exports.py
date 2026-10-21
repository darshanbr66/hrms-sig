"""Employee rows for the employee export (`employee.export`, product-requirements RPT-1).

The `internal` tier only: identity, job placement, dates and status. Personal and sensitive
details are never exported here.
"""

import uuid
from collections.abc import Collection
from datetime import date
from typing import Final

from app.modules.org import public as org
from app.modules.people import repository
from app.modules.people.models import Employee
from app.platform.csvfile import Cell
from app.platform.db import Session

EMPLOYEES_HEADER: Final = (
    "employee_code",
    "name",
    "legal_first_name",
    "legal_last_name",
    "work_email",
    "department",
    "designation",
    "location",
    "manager_code",
    "employment_type",
    "date_of_joining",
    "date_of_exit",
    "status",
)


async def employee_rows(session: Session, employee_ids: Collection[uuid.UUID], on: date) -> list[list[Cell]]:
    if not employee_ids:
        return []
    employees = await session.find(Employee, {"id": {"$in": list(set(employee_ids))}}, sort=[("code_key", 1)])
    ids = [e.id for e in employees]
    jobs = await repository.current_jobs(session, ids, on)
    jobs |= await repository.latest_jobs(session, [i for i in ids if i not in jobs])
    units = await org.unit_names(
        session,
        department_ids={j.department_id for j in jobs.values()},
        designation_ids={j.designation_id for j in jobs.values()},
        location_ids={j.location_id for j in jobs.values()},
    )
    managers = {j.manager_employee_id for j in jobs.values() if j.manager_employee_id}
    manager_codes = (
        {e.id: e.employee_code for e in await session.find(Employee, {"id": {"$in": list(managers)}})}
        if managers
        else {}
    )
    rows: list[list[Cell]] = []
    for employee in employees:
        job = jobs.get(employee.id)
        rows.append(
            [
                employee.employee_code,
                repository.name_of(employee),
                employee.legal_first_name,
                employee.legal_last_name,
                employee.work_email,
                units.departments.get(job.department_id) if job else None,
                units.designations.get(job.designation_id) if job else None,
                units.locations.get(job.location_id) if job else None,
                manager_codes.get(job.manager_employee_id) if job and job.manager_employee_id else None,
                job.employment_type if job else None,
                employee.date_of_joining,
                employee.date_of_exit,
                employee.status,
            ]
        )
    return rows
