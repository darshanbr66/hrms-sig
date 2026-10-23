"""The people module's part of permanently erasing one person's data (owner decision
2026-10-23, docs/database-design.md §9.1).

- The personal tier (date of birth, personal contact details, addresses) and the emergency
  contacts are deleted.
- The employee record stays, because job history, attendance, leave, documents and the audit
  log refer to it: its names, code and work contact details are replaced by placeholders and it
  is marked erased. Joining and exit dates stay, because the period reports and leave years
  that were already worked out depend on them.
- Free-text job notes are overwritten with the erasure marker (`EmployeeJob.__erasable__`); the
  job rows themselves are history and stay (rule 5).
- Status history is append-only (rule 5), so its rows, including the reasons typed when the
  status changed, stay as they are.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from app.modules.people.models import (
    EmergencyContact,
    Employee,
    EmployeeJob,
    EmployeePersonal,
    EmployeeStatusChange,
)
from app.platform.db import ERASED, Session
from app.platform.erasure import Erased, Outcome


@dataclass(frozen=True, slots=True)
class ErasureSubject:
    """Who an erasure is about: the name and code shown to the super admins who request and
    approve it, so both can confirm the person, and the facts its preconditions need."""

    employee_id: uuid.UUID
    name: str
    employee_code: str
    status: str
    date_of_exit: date | None
    erased: bool


def _subject(employee: Employee) -> ErasureSubject:
    return ErasureSubject(
        employee_id=employee.id,
        name=employee.display_name,
        employee_code=employee.employee_code,
        status=employee.status,
        date_of_exit=employee.date_of_exit,
        erased=employee.erased_at is not None,
    )


async def erasure_subject_by_code(session: Session, code: str) -> ErasureSubject | None:
    found = await session.find_one(Employee, {"code_key": code.strip().lower()})
    return _subject(found) if found is not None else None


async def erasure_subject(
    session: Session, employee_id: uuid.UUID, *, lock: bool = False
) -> ErasureSubject | None:
    found = await session.get(Employee, employee_id, for_update=lock)
    return _subject(found) if found is not None else None


async def erase_employee(session: Session, employee_id: uuid.UUID, now: datetime) -> Erased:
    """Erase the people records of one employee, in the caller's transaction."""
    employee = await session.get(Employee, employee_id, for_update=True)
    if employee is None:
        raise RuntimeError("an erasure names a missing employee record")
    result = Erased()
    personal = await session.get(EmployeePersonal, employee_id)
    if personal is not None:
        await session.delete(personal)
    result.add(
        "people.personal_details",
        Outcome.DELETED,
        int(personal is not None),
        "Date of birth, personal email and phone, current and permanent address.",
    )
    contacts = await session.delete_many(EmergencyContact, {"employee_id": employee_id})
    result.add(
        "people.emergency_contacts", Outcome.DELETED, contacts, "Names, relationships and phone numbers."
    )

    employee.employee_code = f"ERASED-{employee.id.hex[:24].upper()}"
    employee.legal_first_name = "Erased"
    employee.legal_last_name = "employee"
    employee.preferred_name = None
    employee.work_email = None
    employee.work_phone = None
    employee.erased_at = now
    employee.updated_at = now
    employee.version += 1
    result.add(
        "people.employee_record",
        Outcome.ANONYMIZED,
        1,
        "Names, employee code, work email and work phone replaced by placeholders. The record stays, "
        "with its joining and exit dates and status, because job, attendance, leave and audit history "
        "refer to it.",
    )

    jobs = await session.find(EmployeeJob, {"employee_id": employee_id})
    noted = [job for job in jobs if job.notes is not None and job.notes != ERASED]
    for job in noted:
        job.notes = ERASED
        job.updated_at = now
    result.add("people.job_notes", Outcome.ANONYMIZED, len(noted), "Free-text notes on job history rows.")
    result.add(
        "people.job_history",
        Outcome.RETAINED,
        len(jobs),
        "Dates, department, designation, location, manager and employment type: employment history that "
        "is never deleted (CLAUDE.md rule 5).",
    )
    statuses = await session.count(EmployeeStatusChange, {"employee_id": employee_id})
    result.add(
        "people.status_history",
        Outcome.RETAINED,
        statuses,
        "Status changes with their dates and the reasons typed then: append-only history (rule 5). A reason "
        "that names the person stays until the retention period Sigvitas confirms.",
    )
    await session.flush()
    return result
