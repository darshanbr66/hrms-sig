"""Obviously fake org and people rows for tests (docs/database-design.md §10).

Codes carry a random suffix because the session database is shared and job history rows
can never be deleted.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.org.models import Department, Designation, Location
from app.modules.people.models import (
    Employee,
    EmployeeJob,
    EmployeeStatus,
    EmploymentType,
    JobChangeReason,
)


def fake_code(prefix: str = "TEST") -> str:
    return f"{prefix}-{secrets.token_hex(4).upper()}"


@dataclass(frozen=True)
class OrgUnits:
    location_id: uuid.UUID
    department_id: uuid.UUID
    designation_id: uuid.UUID


async def create_org_units(session: AsyncSession) -> OrgUnits:
    location = Location(code=fake_code(), name="Test location", time_zone="Asia/Kolkata", country_code="IN")
    department = Department(code=fake_code(), name="Test department")
    designation = Designation(code=fake_code(), name="Test designation")
    session.add_all([location, department, designation])
    await session.flush()
    return OrgUnits(location.id, department.id, designation.id)


async def create_employee(
    session: AsyncSession,
    *,
    joined: date,
    exited: date | None = None,
    status: EmployeeStatus = EmployeeStatus.ACTIVE,
) -> uuid.UUID:
    code = fake_code()
    employee = Employee(
        employee_code=code,
        legal_first_name="Test",
        legal_last_name=code,
        work_email=f"{code.lower()}@dev.example",
        date_of_joining=joined,
        date_of_exit=exited,
        status=status.value if exited is None else EmployeeStatus.EXITED.value,
    )
    session.add(employee)
    await session.flush()
    return employee.id


async def add_job(
    session: AsyncSession,
    units: OrgUnits,
    employee_id: uuid.UUID,
    *,
    manager_id: uuid.UUID | None,
    effective_from: date,
    effective_to: date | None = None,
    reason: JobChangeReason = JobChangeReason.JOINING,
) -> uuid.UUID:
    job = EmployeeJob(
        employee_id=employee_id,
        effective_from=effective_from,
        effective_to=effective_to,
        department_id=units.department_id,
        designation_id=units.designation_id,
        location_id=units.location_id,
        manager_employee_id=manager_id,
        employment_type=EmploymentType.FULL_TIME.value,
        change_reason=reason.value,
    )
    session.add(job)
    await session.flush()
    return job.id
