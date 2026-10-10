"""Employee records (docs/product-requirements.md §6.3, authorization-model.md §3.2).

- The directory (`employee.directory.read`) lists employed, not exited, people with directory
  fields only. Employee records (`employee.profile.read`) are read within the actor's scope:
  their own, their team's, or all (narrowed by an assignment's department or location). A
  record outside the actor's scope is answered as not found.
- HR creates records (`employee.create`) with a first job row, edits profile fields
  (`employee.profile.update.all`), records job changes and status changes
  (`employee.lifecycle.manage`). Every change is audited with what changed.
- Job history and status history are append-only: a job change closes the current row and
  opens a new one; nothing is deleted (CLAUDE.md rule 5).
- The personal tier (`employee.personal.*`) is a separate sub-resource. Reads of another
  person's personal details are audited; changes record which fields changed, never the values.
- Employees edit their own preferred name and personal details through `/me` routes.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.org import public as org
from app.modules.people import repository as repo
from app.modules.people.models import (
    Employee,
    EmployeeJob,
    EmployeePersonal,
    EmployeeStatus,
    EmployeeStatusChange,
    JobChangeReason,
)
from app.platform.audit.records import (
    AuditEvent,
    FieldChange,
    RedactedChange,
    RequestContext,
    ValueChange,
    change_of,
)
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer, Granted
from app.platform.clock import Clock
from app.platform.concurrency import check_version
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType

# Status changes HR can record (product-requirements EMP-5). Exited is final until the rehire
# decision (roadmap §6) is made.
TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    EmployeeStatus.PRE_JOINING: frozenset({EmployeeStatus.ACTIVE, EmployeeStatus.EXITED}),
    EmployeeStatus.ACTIVE: frozenset({EmployeeStatus.ON_NOTICE, EmployeeStatus.EXITED}),
    EmployeeStatus.ON_NOTICE: frozenset({EmployeeStatus.ACTIVE, EmployeeStatus.EXITED}),
    EmployeeStatus.EXITED: frozenset(),
}
ANNIVERSARY_WINDOW: Final = timedelta(days=14)
STARTING_SOON_WINDOW: Final = timedelta(days=30)
RECENT_JOINERS_WINDOW: Final = timedelta(days=30)
MAX_LISTED: Final = 10
PERSONAL_FIELDS: Final = (
    "date_of_birth",
    "personal_email",
    "personal_phone",
    "current_address",
    "permanent_address",
)


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


@dataclass(frozen=True, slots=True)
class Names:
    """Display names of the units and people a set of job rows refers to."""

    units: org.UnitNames
    people: dict[uuid.UUID, str]


@dataclass(frozen=True, slots=True)
class EmployeeRecord:
    employee: Employee
    job: EmployeeJob | None
    names: Names


@dataclass(frozen=True, slots=True)
class EmployeeListing:
    records: list[EmployeeRecord]
    next_after: tuple[str, uuid.UUID] | None


@dataclass(frozen=True, slots=True)
class JobHistory:
    jobs: list[EmployeeJob]
    names: Names
    # Managers see job history without the HR notes.
    include_notes: bool


@dataclass(frozen=True, slots=True)
class Statistics:
    employed: int
    active: int
    on_notice: int
    pre_joining: int
    joined_recently: int
    starting_soon: list[tuple[uuid.UUID, str, date]]
    anniversaries: list[tuple[uuid.UUID, str, date, int]]


@dataclass(frozen=True, slots=True)
class NewEmployee:
    values: Mapping[str, Any]
    department_id: uuid.UUID
    designation_id: uuid.UUID
    location_id: uuid.UUID
    manager_employee_id: uuid.UUID | None
    employment_type: str


@dataclass(frozen=True, slots=True)
class JobChangeRequest:
    effective_from: date
    department_id: uuid.UUID
    designation_id: uuid.UUID
    location_id: uuid.UUID
    manager_employee_id: uuid.UUID | None
    employment_type: str
    change_reason: str
    notes: str | None


async def _names(session: AsyncSession, jobs: Sequence[EmployeeJob]) -> Names:
    units = await org.unit_names(
        session,
        department_ids={job.department_id for job in jobs},
        designation_ids={job.designation_id for job in jobs},
        location_ids={job.location_id for job in jobs},
    )
    managers = {job.manager_employee_id for job in jobs if job.manager_employee_id is not None}
    return Names(units, await repo.employee_names(session, managers))


class PeopleService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    # --- directory and lists -------------------------------------------------------------------

    async def directory(
        self, filters: repo.EmployeeFilters, after: tuple[str, uuid.UUID] | None, limit: int
    ) -> EmployeeListing:
        today = self._authz.today()
        async with self._db.unit_of_work() as session:
            rows = await repo.directory(session, filters, on=today, after=after, limit=limit)
            return await self._listing(session, rows, limit, today)

    async def employees(
        self, actor: Actor, filters: repo.EmployeeFilters, after: tuple[str, uuid.UUID] | None, limit: int
    ) -> EmployeeListing:
        today = self._authz.today()
        scope = self._authz.scope_filter(actor, "employee.profile.read", Employee.id)
        async with self._db.unit_of_work() as session:
            rows = await repo.list_employees(session, scope, filters, on=today, after=after, limit=limit)
            return await self._listing(session, rows, limit, today)

    async def _listing(
        self, session: AsyncSession, rows: list[tuple[Employee, str]], limit: int, today: date
    ) -> EmployeeListing:
        page = rows[:limit]
        employees = [employee for employee, _ in page]
        jobs = await self._jobs_for(session, employees, today)
        names = await _names(session, list(jobs.values()))
        records = [EmployeeRecord(employee, jobs.get(employee.id), names) for employee in employees]
        next_after = (page[-1][1], page[-1][0].id) if len(rows) > limit else None
        return EmployeeListing(records, next_after)

    @staticmethod
    async def _jobs_for(
        session: AsyncSession, employees: Sequence[Employee], today: date
    ) -> dict[uuid.UUID, EmployeeJob]:
        """The job that applies today, else the latest one (future joiners, people who left)."""
        ids = [employee.id for employee in employees]
        jobs = await repo.current_jobs(session, ids, today)
        missing = [employee_id for employee_id in ids if employee_id not in jobs]
        jobs.update(await repo.latest_jobs(session, missing))
        return jobs

    async def statistics(self, actor: Actor) -> Statistics:
        today = self._authz.today()
        scope = self._authz.scope_filter(actor, "employee.profile.read", Employee.id)
        async with self._db.unit_of_work() as session:
            rows = await repo.statistics_rows(session, scope)
        employed = [
            row
            for row in rows
            if row.date_of_joining <= today and (row.date_of_exit is None or row.date_of_exit >= today)
        ]
        starting = sorted(
            (
                (row.id, row.name, row.date_of_joining)
                for row in rows
                if row.status == EmployeeStatus.PRE_JOINING
                and today < row.date_of_joining <= today + STARTING_SOON_WINDOW
            ),
            key=lambda item: (item[2], item[1]),
        )
        anniversaries = sorted(
            (
                item
                for row in employed
                if (item := _anniversary(row.id, row.name, row.date_of_joining, today)) is not None
            ),
            key=lambda item: (item[2], item[1]),
        )
        return Statistics(
            employed=len(employed),
            active=sum(row.status == EmployeeStatus.ACTIVE for row in employed),
            on_notice=sum(row.status == EmployeeStatus.ON_NOTICE for row in employed),
            pre_joining=sum(row.status == EmployeeStatus.PRE_JOINING for row in rows),
            joined_recently=sum(
                today - RECENT_JOINERS_WINDOW <= row.date_of_joining <= today for row in employed
            ),
            starting_soon=starting[:MAX_LISTED],
            anniversaries=anniversaries[:MAX_LISTED],
        )

    # --- one record ----------------------------------------------------------------------------

    async def get(self, actor: Actor, employee_id: uuid.UUID) -> EmployeeRecord:
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "employee.profile.read", subject_employee_id=employee_id, hide_existence=True
            )
            return await self._record(session, employee_id)

    async def own(self, actor: Actor) -> EmployeeRecord:
        employee_id = _own_employee(actor)
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "employee.profile.read.self", subject_employee_id=employee_id
            )
            return await self._record(session, employee_id)

    async def _record(self, session: AsyncSession, employee_id: uuid.UUID) -> EmployeeRecord:
        employee = await session.get(Employee, employee_id, populate_existing=True)
        if employee is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        jobs = await self._jobs_for(session, [employee], self._authz.today())
        job = jobs.get(employee.id)
        return EmployeeRecord(employee, job, await _names(session, [job] if job else []))

    async def jobs(self, actor: Actor, employee_id: uuid.UUID) -> JobHistory:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, "employee.profile.read", subject_employee_id=employee_id, hide_existence=True
            )
            jobs = await repo.job_history(session, employee_id)
            return JobHistory(
                jobs, await _names(session, jobs), include_notes=not granted.key.endswith(".team")
            )

    async def status_history(self, actor: Actor, employee_id: uuid.UUID) -> list[EmployeeStatusChange]:
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "employee.profile.read", subject_employee_id=employee_id, hide_existence=True
            )
            return await repo.status_history(session, employee_id)

    # --- changes -------------------------------------------------------------------------------

    async def create(self, actor: Actor, context: RequestContext, request: NewEmployee) -> EmployeeRecord:
        today = self._authz.today()
        joining: date = request.values["date_of_joining"]
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "employee.create")
            # A department- or location-restricted HR assignment creates employees only there.
            await self._require_placement(session, actor, granted, request.department_id, request.location_id)
            await _check_units(session, request.department_id, request.designation_id, request.location_id)
            if request.manager_employee_id is not None:
                await self._check_manager(session, None, request.manager_employee_id, joining)
            status = EmployeeStatus.PRE_JOINING if joining > today else EmployeeStatus.ACTIVE
            employee = Employee(**request.values, status=status.value)
            session.add(employee)
            await _flush(session)
            session.add(
                EmployeeJob(
                    employee_id=employee.id,
                    effective_from=joining,
                    department_id=request.department_id,
                    designation_id=request.designation_id,
                    location_id=request.location_id,
                    manager_employee_id=request.manager_employee_id,
                    employment_type=request.employment_type,
                    change_reason=JobChangeReason.JOINING.value,
                )
            )
            await session.flush()
            changes: dict[str, FieldChange] = {
                name: change_of(name, None, value)
                for name, value in request.values.items()
                if value is not None
            }
            changes |= {
                "status": ValueChange(None, status.value),
                "department_id": ValueChange(None, request.department_id),
                "designation_id": ValueChange(None, request.designation_id),
                "location_id": ValueChange(None, request.location_id),
                "manager_employee_id": ValueChange(None, request.manager_employee_id),
                "employment_type": ValueChange(None, request.employment_type),
            }
            await self._audit.record(
                session,
                AuditEvent(
                    action="employee.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="employee",
                    target_id=employee.id,
                    subject_employee_id=employee.id,
                    changes=changes,
                ),
            )
            return await self._record(session, employee.id)

    async def update(
        self,
        actor: Actor,
        context: RequestContext,
        employee_id: uuid.UUID,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> EmployeeRecord:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session,
                actor,
                "employee.profile.update.all",
                subject_employee_id=employee_id,
                hide_existence=True,
            )
            for required in ("employee_code", "legal_first_name"):
                if required in values and values[required] is None:
                    raise field_error(required, "employee.required", "Enter a value.")
            return await self._apply(
                session,
                actor,
                context,
                granted,
                employee_id,
                expected_version=expected_version,
                values=values,
            )

    async def update_own(
        self, actor: Actor, context: RequestContext, expected_version: int, values: Mapping[str, Any]
    ) -> EmployeeRecord:
        employee_id = _own_employee(actor)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, "employee.profile.update.self", subject_employee_id=employee_id
            )
            return await self._apply(
                session,
                actor,
                context,
                granted,
                employee_id,
                expected_version=expected_version,
                values=values,
            )

    async def _apply(
        self,
        session: AsyncSession,
        actor: Actor,
        context: RequestContext,
        granted: Granted,
        employee_id: uuid.UUID,
        *,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> EmployeeRecord:
        employee = await session.get(Employee, employee_id, with_for_update=True, populate_existing=True)
        if employee is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        check_version(employee.version, expected_version)
        changes: dict[str, FieldChange] = {
            name: change_of(name, getattr(employee, name), value)
            for name, value in values.items()
            if getattr(employee, name) != value
        }
        if changes:
            for name, value in values.items():
                setattr(employee, name, value)
            employee.version += 1
            employee.updated_at = self._clock.now()
            await _flush(session)
            await self._audit.record(
                session,
                AuditEvent(
                    action="employee.profile.updated",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="employee",
                    target_id=employee.id,
                    subject_employee_id=employee.id,
                    changes=changes,
                ),
            )
        return await self._record(session, employee.id)

    async def change_job(
        self, actor: Actor, context: RequestContext, employee_id: uuid.UUID, change: JobChangeRequest
    ) -> list[EmployeeJob]:
        """Closes the open job row on the change's date and opens a new one from it."""
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session,
                actor,
                "employee.lifecycle.manage",
                subject_employee_id=employee_id,
                hide_existence=True,
            )
            await self._require_placement(session, actor, granted, change.department_id, change.location_id)
            employee = await session.get(Employee, employee_id, with_for_update=True, populate_existing=True)
            if employee is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if employee.status == EmployeeStatus.EXITED:
                raise conflict(
                    "employee.exited", "This person has left. Their job history can no longer change."
                )
            history = await repo.job_history(session, employee_id)
            latest = history[0] if history else None
            if latest is None or latest.effective_to is not None:
                raise conflict("employee.no_open_job", "This person has no current job to change.")
            if change.effective_from <= latest.effective_from:
                raise field_error(
                    "effective_from",
                    "job.effective_from_too_early",
                    f"Choose a date after {latest.effective_from.isoformat()}, when the current job started.",
                )
            await _check_units(session, change.department_id, change.designation_id, change.location_id)
            if change.manager_employee_id is not None:
                await self._check_manager(
                    session, employee_id, change.manager_employee_id, change.effective_from
                )
            latest.effective_to = change.effective_from
            latest.updated_at = self._clock.now()
            new_job = EmployeeJob(
                employee_id=employee_id,
                effective_from=change.effective_from,
                department_id=change.department_id,
                designation_id=change.designation_id,
                location_id=change.location_id,
                manager_employee_id=change.manager_employee_id,
                employment_type=change.employment_type,
                change_reason=change.change_reason,
                notes=change.notes,
            )
            session.add(new_job)
            await session.flush()
            changes: dict[str, FieldChange] = {
                name: ValueChange(getattr(latest, name), getattr(new_job, name))
                for name in (
                    "department_id",
                    "designation_id",
                    "location_id",
                    "manager_employee_id",
                    "employment_type",
                )
                if getattr(latest, name) != getattr(new_job, name)
            } or {"effective_from": ValueChange(latest.effective_from, change.effective_from)}
            await self._audit.record(
                session,
                AuditEvent(
                    action="employee.job.changed",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="employee_job",
                    target_id=new_job.id,
                    subject_employee_id=employee_id,
                    changes=changes,
                    reason=change.change_reason,
                ),
            )
            return await repo.job_history(session, employee_id)

    async def change_status(
        self,
        actor: Actor,
        context: RequestContext,
        employee_id: uuid.UUID,
        *,
        to_status: str,
        effective_date: date,
        reason: str,
    ) -> EmployeeStatusChange:
        today = self._authz.today()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session,
                actor,
                "employee.lifecycle.manage",
                subject_employee_id=employee_id,
                hide_existence=True,
            )
            employee = await session.get(Employee, employee_id, with_for_update=True, populate_existing=True)
            if employee is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            from_status = employee.status
            if to_status not in TRANSITIONS[EmployeeStatus(from_status)]:
                raise conflict(
                    "employee.status_transition",
                    f"A record that is {_words(from_status)} cannot change to {_words(to_status)}.",
                )
            changes: dict[str, FieldChange] = {"status": ValueChange(from_status, to_status)}
            if to_status == EmployeeStatus.EXITED:
                if effective_date > today:
                    raise field_error(
                        "effective_date",
                        "employee.exit_in_future",
                        "Record the exit on or after the last working day. "
                        "Until then, mark the person as on notice.",
                    )
                if effective_date < employee.date_of_joining:
                    raise field_error(
                        "effective_date",
                        "employee.exit_before_joining",
                        "The last working day cannot be before joining.",
                    )
                changes["date_of_exit"] = ValueChange(employee.date_of_exit, effective_date)
                employee.date_of_exit = effective_date
            elif to_status == EmployeeStatus.ACTIVE and employee.date_of_joining > today:
                raise field_error(
                    "to_status",
                    "employee.not_joined",
                    "This person joins on a later date. Mark them active from then.",
                )
            employee.status = to_status
            employee.version += 1
            employee.updated_at = self._clock.now()
            record = EmployeeStatusChange(
                employee_id=employee_id,
                from_status=from_status,
                to_status=to_status,
                effective_date=effective_date,
                reason=reason,
                changed_by=actor.user_id,
            )
            session.add(record)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="employee.status.changed",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="employee",
                    target_id=employee_id,
                    subject_employee_id=employee_id,
                    changes=changes,
                    reason=reason,
                ),
            )
            await session.refresh(record)
            return record

    # --- personal tier -------------------------------------------------------------------------

    async def personal(
        self, actor: Actor, context: RequestContext, employee_id: uuid.UUID | None
    ) -> EmployeePersonal | None:
        """Another employee's personal details (`employee_id` given), or the actor's own."""
        subject = employee_id or _own_employee(actor)
        permission = "employee.personal.read" if employee_id else "employee.personal.read.self"
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, permission, subject_employee_id=subject, hide_existence=True
            )
            if not await repo.employee_exists(session, subject):
                raise ProblemError(ProblemType.NOT_FOUND)
            record = await session.get(EmployeePersonal, subject)
            if granted.audit_reads:
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="employee.personal.read",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=granted.key,
                        target_type="employee",
                        target_id=subject,
                        subject_employee_id=subject,
                    ),
                )
            return record

    async def update_personal(
        self,
        actor: Actor,
        context: RequestContext,
        employee_id: uuid.UUID | None,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> EmployeePersonal:
        subject = employee_id or _own_employee(actor)
        permission = "employee.personal.update.all" if employee_id else "employee.personal.update.self"
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, permission, subject_employee_id=subject, hide_existence=True
            )
            employee = await session.get(Employee, subject, with_for_update=True, populate_existing=True)
            if employee is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            today = self._authz.today()
            birth = values.get("date_of_birth")
            if birth is not None and birth >= today:
                raise field_error("date_of_birth", "personal.birth_in_future", "Enter a date in the past.")
            record = await session.get(
                EmployeePersonal, subject, with_for_update=True, populate_existing=True
            )
            check_version(record.version if record else 0, expected_version)
            if record is None:
                record = EmployeePersonal(employee_id=subject)
                session.add(record)
                changed = [name for name, value in values.items() if value is not None]
            else:
                changed = [name for name, value in values.items() if getattr(record, name) != value]
                record.version += 1
                record.updated_at = self._clock.now()
            for name, value in values.items():
                setattr(record, name, value)
            await session.flush()
            if changed:
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="employee.personal.updated",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=granted.key,
                        target_type="employee",
                        target_id=subject,
                        subject_employee_id=subject,
                        # Personal values never reach the audit log (CLAUDE.md rule 6).
                        changes=dict.fromkeys(changed, RedactedChange()),
                    ),
                )
            await session.refresh(record)
            return record

    # --- rules ---------------------------------------------------------------------------------

    async def _require_placement(
        self,
        session: AsyncSession,
        actor: Actor,
        granted: Granted,
        department_id: uuid.UUID,
        location_id: uuid.UUID,
    ) -> None:
        """An HR assignment restricted to a department or location places people only there."""
        constraints = actor.grants.get(granted.key, ())
        if any(constraint.unrestricted for constraint in constraints):
            return
        if not any(
            (constraint.department_id in (None, department_id))
            and (constraint.location_id in (None, location_id))
            for constraint in constraints
        ):
            raise AccessDenied(code="employee.outside_assignment")

    async def _check_manager(
        self, session: AsyncSession, employee_id: uuid.UUID | None, manager_id: uuid.UUID, on: date
    ) -> None:
        if manager_id == employee_id:
            raise field_error(
                "manager_employee_id", "job.manager_self", "A person cannot report to themselves."
            )
        if not await repo.is_employed(session, manager_id, on):
            raise field_error(
                "manager_employee_id",
                "job.manager_not_employed",
                "Choose a manager who is employed on the date the change takes effect.",
            )
        if employee_id is not None and await repo.is_team_member(session, employee_id, manager_id, on):
            raise field_error(
                "manager_employee_id",
                "job.manager_cycle",
                "This manager reports to this person, directly or indirectly. Choose someone else.",
            )


def _words(status: str) -> str:
    return status.replace("_", " ")


def _own_employee(actor: Actor) -> uuid.UUID:
    if actor.employee_id is None:
        raise ProblemError(ProblemType.NOT_FOUND, detail="Your account is not linked to an employee record.")
    return actor.employee_id


def _anniversary(
    employee_id: uuid.UUID, name: str, joined: date, today: date
) -> tuple[uuid.UUID, str, date, int] | None:
    """The next work anniversary within the window, for people with at least one full year."""
    for year in (today.year, today.year + 1):
        try:
            candidate = joined.replace(year=year)
        except ValueError:  # 29 February in a non-leap year
            candidate = date(year, 3, 1)
        if today <= candidate <= today + ANNIVERSARY_WINDOW and year > joined.year:
            return employee_id, name, candidate, year - joined.year
    return None


async def _check_units(
    session: AsyncSession, department_id: uuid.UUID, designation_id: uuid.UUID, location_id: uuid.UUID
) -> None:
    inactive = await org.inactive_units(
        session, department_id=department_id, designation_id=designation_id, location_id=location_id
    )
    if inactive:
        raise ProblemError(
            ProblemType.VALIDATION_ERROR,
            extensions={
                "errors": [
                    {
                        "field": f"job.{name}",
                        "code": "job.unit_inactive",
                        "message": "Choose an active option.",
                    }
                    for name in inactive
                ]
            },
        )


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        message = str(exc.orig)
        if "uq_employees_employee_code" in message:
            raise field_error(
                "employee_code", "employee.code_in_use", "Another employee already has this code."
            ) from exc
        if "uq_employees_work_email" in message:
            raise field_error(
                "work_email", "employee.email_in_use", "Another employee already has this work email."
            ) from exc
        raise
