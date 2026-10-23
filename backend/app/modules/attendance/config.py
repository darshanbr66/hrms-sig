"""Shift and attendance policy configuration, shift assignments and attendance locks
(product-requirements ATT-9, ATT-11, ATT-12; authorization-model §3.3).

- Shifts and their versions (`shift.manage`): when work is expected and which days are off.
  A new version starts on a date and closes the open one; a version that already started is
  never edited, so past days keep the values they were judged with.
- Location default shifts (`shift.manage`) and employee assignments (`shift.assign.all`,
  within the assignment's department or location constraint): effective-dated, a new one
  closes the open one.
- Attendance policies (`attendance.policy.manage`): effective-dated per scope (organization,
  location, shift); a new version closes the open one of the same scope. Every value is
  entered by HR; nothing is assumed.
- Attendance locks (`attendance.policy.manage`): close a date range to corrections except by
  `attendance.correction.approve.all` holders; a lock is released with a reason, never deleted.
- The setup view lists who has no shift, and which shifts and locations have no policy today
  (ATT-12), within the reader's attendance scope.

Every change is audited with old and new values (configuration holds no personal data).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Final

from app.modules.attendance.models import (
    AttendanceLock,
    AttendancePolicy,
    LocationDefaultShift,
    PolicyScope,
    Shift,
    ShiftAssignment,
    ShiftStatus,
    ShiftVersion,
)
from app.modules.attendance.schedule import Schedule
from app.modules.org import public as org
from app.modules.people import public as people
from app.platform.audit.records import AuditEvent, FieldChange, RequestContext, ValueChange, change_of
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer
from app.platform.clock import Clock
from app.platform.concurrency import check_version
from app.platform.db import CASE_INSENSITIVE, Database, Session, UniqueViolationError
from app.platform.errors import ProblemError, ProblemType

MAX_BULK: Final = 500
POLICY_FIELDS: Final = (
    "grace_minutes",
    "early_departure_grace_minutes",
    "duration_basis",
    "full_day_min_minutes",
    "half_day_min_minutes",
    "max_break_minutes",
    "breaks_count_as_work",
    "break_excess_action",
    "overtime_enabled",
    "overtime_basis",
    "overtime_min_minutes",
    "overtime_requires_approval",
    "incomplete_after_minutes",
    "allow_multiple_sessions",
)


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def _changes(values: Mapping[str, Any], before: Any = None) -> dict[str, FieldChange]:
    return {
        name: change_of(name, getattr(before, name) if before is not None else None, value)
        for name, value in values.items()
        if before is None or getattr(before, name) != value
    }


async def supersede(
    session: Session, model: Any, series: dict[str, Any], effective_from: date, what: str
) -> uuid.UUID | None:
    """Close the open row of one series at `effective_from`. Refuses when a row of the series
    starts on or after that date, or ends after it: history is never rewritten. The caller holds
    the series' lock, so two versions never overlap (formerly an exclusion constraint)."""
    rows: list[Any] = await session.find(
        model,
        {
            **series,
            "$or": [
                {"effective_to": None},
                {"effective_from": {"$gte": effective_from}},
                {"effective_to": {"$gt": effective_from}},
            ],
        },
    )
    current = [row for row in rows if row.effective_from < effective_from and row.effective_to is None]
    if len(rows) != len(current):
        raise conflict(
            "attendance.version_overlap",
            f"A version of this {what} already starts on or after that date, or ends after it. "
            "Choose a later start date.",
        )
    for row in current:
        row.effective_to = effective_from
    await session.flush()
    return current[0].id if current else None


def _weekly_offs_json(weekly_offs: Sequence[tuple[int, Sequence[int] | None]]) -> list[dict[str, Any]]:
    return [
        {"weekday": weekday, "weeks": sorted(set(weeks)) if weeks else None} for weekday, weeks in weekly_offs
    ]


@dataclass(frozen=True, slots=True)
class ShiftRecord:
    shift: Shift
    versions: list[ShiftVersion]


@dataclass(frozen=True, slots=True)
class VersionValues:
    effective_from: date
    start_time: time
    end_time: time
    # (weekday 1-7, weeks of the month or None for every week)
    weekly_offs: tuple[tuple[int, tuple[int, ...] | None], ...]


@dataclass(frozen=True, slots=True)
class AssignmentRecord:
    assignment: ShiftAssignment
    shift_name: str


@dataclass(frozen=True, slots=True)
class DefaultRecord:
    default: LocationDefaultShift
    shift_name: str
    location_name: str


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    policy: AttendancePolicy
    scope_name: str | None


@dataclass(frozen=True, slots=True)
class BulkResult:
    assigned: int
    skipped_not_permitted: int


@dataclass(frozen=True, slots=True)
class SetupGaps:
    on: date
    shifts: int
    policies: int
    employees_in_scope: int
    without_shift: list[people.Colleague]
    without_policy: list[people.Colleague]
    organization_policy: bool


class ConfigService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    # --- shifts --------------------------------------------------------------------------------

    async def shifts(self, *, include_archived: bool) -> list[ShiftRecord]:
        query = {} if include_archived else {"status": ShiftStatus.ACTIVE.value}
        async with self._db.unit_of_work() as session:
            shifts = await session.find(
                Shift, query, sort=[("name", 1), ("id", 1)], collation=CASE_INSENSITIVE
            )
            return await self._with_versions(session, shifts)

    @staticmethod
    async def _with_versions(session: Session, shifts: Sequence[Shift]) -> list[ShiftRecord]:
        versions: dict[uuid.UUID, list[ShiftVersion]] = {shift.id: [] for shift in shifts}
        if shifts:
            for version in await session.find(
                ShiftVersion,
                {"shift_id": {"$in": list(versions)}},
                sort=[("shift_id", 1), ("effective_from", -1)],
            ):
                versions[version.shift_id].append(version)
        return [ShiftRecord(shift, versions[shift.id]) for shift in shifts]

    async def create_shift(
        self, actor: Actor, context: RequestContext, values: Mapping[str, Any], first: VersionValues
    ) -> ShiftRecord:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "shift.manage")
            shift = Shift(**values)
            session.add(shift)
            try:
                await session.flush()
            except UniqueViolationError as exc:
                raise conflict("shift.code_in_use", "Another shift already uses this code.") from exc
            version = self._version(shift.id, 1, first, actor.user_id)
            session.add(version)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="shift.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="shift",
                    target_id=shift.id,
                    changes={**_changes(values), **self._version_changes(first)},
                ),
            )
            await session.refresh(shift)
            return (await self._with_versions(session, [shift]))[0]

    @staticmethod
    def _version(shift_id: uuid.UUID, number: int, values: VersionValues, user_id: uuid.UUID) -> ShiftVersion:
        if values.start_time == values.end_time:
            raise field_error(
                "end_time", "shift.same_times", "The shift must end at a different time than it starts."
            )
        return ShiftVersion(
            shift_id=shift_id,
            version_no=number,
            effective_from=values.effective_from,
            start_time=values.start_time,
            end_time=values.end_time,
            crosses_midnight=values.end_time < values.start_time,
            weekly_offs=_weekly_offs_json(values.weekly_offs),
            created_by=user_id,
        )

    @staticmethod
    def _version_changes(values: VersionValues) -> dict[str, FieldChange]:
        return {
            "effective_from": ValueChange(None, values.effective_from),
            "start_time": ValueChange(None, values.start_time.isoformat()),
            "end_time": ValueChange(None, values.end_time.isoformat()),
            "weekly_offs": ValueChange(
                None,
                ", ".join(
                    f"{weekday}" + (f" (weeks {'/'.join(map(str, weeks))})" if weeks else "")
                    for weekday, weeks in values.weekly_offs
                )
                or "none",
            ),
        }

    async def update_shift(
        self,
        actor: Actor,
        context: RequestContext,
        shift_id: uuid.UUID,
        *,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> ShiftRecord:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "shift.manage")
            shift = await session.get(Shift, shift_id, for_update=True)
            if shift is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            check_version(shift.version, expected_version)
            if values.get("status") == ShiftStatus.ARCHIVED and shift.status != ShiftStatus.ARCHIVED:
                await self._refuse_archive_in_use(session, shift.id)
            changes = _changes(values, shift)
            if changes:
                for name, value in values.items():
                    setattr(shift, name, value)
                shift.version += 1
                shift.updated_at = self._clock.now()
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="shift.updated",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=granted.key,
                        target_type="shift",
                        target_id=shift.id,
                        changes=changes,
                    ),
                )
            await session.flush()
            await session.refresh(shift)
            return (await self._with_versions(session, [shift]))[0]

    async def _refuse_archive_in_use(self, session: Session, shift_id: uuid.UUID) -> None:
        today = self._authz.today()
        in_use = {"shift_id": shift_id, "$or": [{"effective_to": None}, {"effective_to": {"$gt": today}}]}
        assignments = await session.count(ShiftAssignment, in_use)
        defaults = await session.count(LocationDefaultShift, in_use)
        if assignments or defaults:
            raise conflict(
                "shift.in_use",
                f"This shift is still assigned to {assignments} people and is the default of {defaults} "
                "locations, now or from a later date. Assign another shift first.",
            )

    async def add_version(
        self, actor: Actor, context: RequestContext, shift_id: uuid.UUID, values: VersionValues
    ) -> ShiftRecord:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "shift.manage")
            shift = await session.get(Shift, shift_id, for_update=True)
            if shift is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if shift.status != ShiftStatus.ACTIVE:
                raise conflict("shift.archived", "This shift is archived. Make it active first.")
            replaced = await supersede(
                session, ShiftVersion, {"shift_id": shift.id}, values.effective_from, "shift"
            )
            latest = await session.find_one(ShiftVersion, {"shift_id": shift.id}, sort=[("version_no", -1)])
            number = (latest.version_no if latest else 0) + 1
            version = self._version(shift.id, number, values, actor.user_id)
            session.add(version)
            shift.version += 1
            shift.updated_at = self._clock.now()
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="shift.version.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="shift",
                    target_id=shift.id,
                    changes={
                        **self._version_changes(values),
                        "version_no": ValueChange(None, number),
                        "replaces": ValueChange(None, replaced),
                    },
                ),
            )
            await session.refresh(shift)
            return (await self._with_versions(session, [shift]))[0]

    async def _active_shift(self, session: Session, shift_id: uuid.UUID) -> Shift:
        shift = await session.get(Shift, shift_id)
        if shift is None or shift.status != ShiftStatus.ACTIVE:
            raise field_error("shift_id", "shift.unknown", "Choose an active shift.")
        return shift

    # --- location defaults ---------------------------------------------------------------------

    async def defaults(self) -> list[DefaultRecord]:
        async with self._db.unit_of_work() as session:
            rows = await session.find(
                LocationDefaultShift, {}, sort=[("location_id", 1), ("effective_from", -1)]
            )
            return await self._default_records(session, rows)

    @staticmethod
    async def _default_records(session: Session, rows: Sequence[LocationDefaultShift]) -> list[DefaultRecord]:
        names = await _shift_names(session, {r.shift_id for r in rows})
        locations = await org.unit_names(session, location_ids={r.location_id for r in rows})
        return [
            DefaultRecord(row, names.get(row.shift_id, ""), locations.locations.get(row.location_id, ""))
            for row in rows
        ]

    async def set_default(
        self,
        actor: Actor,
        context: RequestContext,
        location_id: uuid.UUID,
        shift_id: uuid.UUID,
        effective_from: date,
    ) -> DefaultRecord:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "shift.manage")
            if not await org.location_time_zones(session, [location_id]):
                raise field_error(
                    "location_id", "attendance.unknown_location", "Choose an existing location."
                )
            await self._active_shift(session, shift_id)
            await session.lock(f"attendance_default:{location_id}")
            replaced = await supersede(
                session,
                LocationDefaultShift,
                {"location_id": location_id},
                effective_from,
                "location's default shift",
            )
            row = LocationDefaultShift(
                location_id=location_id,
                shift_id=shift_id,
                effective_from=effective_from,
                created_by=actor.user_id,
            )
            session.add(row)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="shift.location_default.set",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="location",
                    target_id=location_id,
                    changes={
                        "shift_id": ValueChange(None, shift_id),
                        "effective_from": ValueChange(None, effective_from),
                        "replaces": ValueChange(None, replaced),
                    },
                ),
            )
            await session.refresh(row)
            return (await self._default_records(session, [row]))[0]

    # --- assignments ---------------------------------------------------------------------------

    async def assignments(self, actor: Actor, employee_id: uuid.UUID) -> list[AssignmentRecord]:
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "attendance.read", subject_employee_id=employee_id, hide_existence=True
            )
            rows = await session.find(
                ShiftAssignment, {"employee_id": employee_id}, sort=[("effective_from", -1)]
            )
            names = await _shift_names(session, {r.shift_id for r in rows})
            return [AssignmentRecord(row, names.get(row.shift_id, "")) for row in rows]

    async def assign(
        self,
        actor: Actor,
        context: RequestContext,
        employee_ids: Sequence[uuid.UUID],
        shift_id: uuid.UUID,
        effective_from: date,
    ) -> BulkResult:
        """Assign a shift from a date to each employee the actor may assign; the others are
        counted, not changed. All or nothing for the permitted ones."""
        if not 1 <= len(set(employee_ids)) <= MAX_BULK:
            raise field_error("employee_ids", "attendance.bulk_size", f"Choose 1 to {MAX_BULK} people.")
        assigned = skipped = 0
        async with self._db.unit_of_work() as session:
            await self._active_shift(session, shift_id)
            for employee_id in sorted(set(employee_ids)):
                try:
                    granted = await self._authz.require(
                        session, actor, "shift.assign.all", subject_employee_id=employee_id
                    )
                except AccessDenied:
                    skipped += 1
                    continue
                if not await people.employee_exists(session, employee_id):
                    skipped += 1
                    continue
                await session.lock(f"attendance_assignment:{employee_id}")
                replaced = await supersede(
                    session,
                    ShiftAssignment,
                    {"employee_id": employee_id},
                    effective_from,
                    "person's shift",
                )
                row = ShiftAssignment(
                    employee_id=employee_id,
                    shift_id=shift_id,
                    effective_from=effective_from,
                    created_by=actor.user_id,
                )
                session.add(row)
                await session.flush()
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="shift.assigned",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=granted.key,
                        target_type="shift_assignment",
                        target_id=row.id,
                        subject_employee_id=employee_id,
                        changes={
                            "shift_id": ValueChange(None, shift_id),
                            "effective_from": ValueChange(None, effective_from),
                            "replaces": ValueChange(None, replaced),
                        },
                    ),
                )
                assigned += 1
            if assigned == 0 and skipped:
                raise AccessDenied
        return BulkResult(assigned, skipped)

    # --- policies ------------------------------------------------------------------------------

    async def policies(self) -> list[PolicyRecord]:
        async with self._db.unit_of_work() as session:
            rows = await session.find(
                AttendancePolicy, {}, sort=[("scope_type", 1), ("scope_id", 1), ("effective_from", -1)]
            )
            return await self._policy_records(session, rows)

    @staticmethod
    async def _policy_records(session: Session, rows: Sequence[AttendancePolicy]) -> list[PolicyRecord]:
        shift_ids = {r.scope_id for r in rows if r.scope_type == PolicyScope.SHIFT and r.scope_id}
        location_ids = {r.scope_id for r in rows if r.scope_type == PolicyScope.LOCATION and r.scope_id}
        shifts = await _shift_names(session, shift_ids)
        locations = (await org.unit_names(session, location_ids=location_ids)).locations
        return [
            PolicyRecord(
                row,
                (shifts if row.scope_type == PolicyScope.SHIFT else locations).get(row.scope_id)
                if row.scope_id is not None
                else None,
            )
            for row in rows
        ]

    async def create_policy(
        self, actor: Actor, context: RequestContext, values: Mapping[str, Any]
    ) -> PolicyRecord:
        self._check_policy(values)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "attendance.policy.manage")
            scope_type: str = values["scope_type"]
            scope_id: uuid.UUID | None = values.get("scope_id")
            if (
                scope_type == PolicyScope.LOCATION
                and scope_id is not None
                and not await org.location_time_zones(session, [scope_id])
            ):
                raise field_error("scope_id", "attendance.unknown_location", "Choose an existing location.")
            if scope_type == PolicyScope.SHIFT and scope_id is not None:
                await self._active_shift(session, scope_id)
            await session.lock(f"attendance_policy:{scope_type}:{scope_id}")
            same_scope = {"scope_type": scope_type, "scope_id": scope_id}
            replaced = await supersede(
                session, AttendancePolicy, same_scope, values["effective_from"], "policy"
            )
            policy = AttendancePolicy(**values, created_by=actor.user_id)
            session.add(policy)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.policy.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_policy",
                    target_id=policy.id,
                    changes={**_changes(values), "replaces": ValueChange(None, replaced)},
                ),
            )
            await session.refresh(policy)
            return (await self._policy_records(session, [policy]))[0]

    @staticmethod
    def _check_policy(values: Mapping[str, Any]) -> None:
        if (values["scope_type"] == PolicyScope.ORGANIZATION) != (values.get("scope_id") is None):
            raise field_error(
                "scope_id",
                "attendance.policy_scope",
                "Choose a location or shift, or apply it to the organization.",
            )
        if values["half_day_min_minutes"] > values["full_day_min_minutes"]:
            raise field_error(
                "half_day_min_minutes",
                "attendance.half_day_above_full_day",
                "The half-day minimum cannot be more than the full-day minimum.",
            )
        overtime = values["overtime_enabled"]
        if overtime and (values.get("overtime_basis") is None or values.get("overtime_min_minutes") is None):
            raise field_error(
                "overtime_basis",
                "attendance.overtime_incomplete",
                "Choose how overtime is measured and from how many minutes it counts.",
            )
        if not overtime and (
            values.get("overtime_basis") is not None
            or values.get("overtime_min_minutes") is not None
            or values["overtime_requires_approval"]
        ):
            raise field_error(
                "overtime_enabled",
                "attendance.overtime_disabled",
                "Turn overtime on to set how it is measured, or clear the overtime settings.",
            )

    # --- locks ---------------------------------------------------------------------------------

    async def locks(self, actor: Actor) -> list[AttendanceLock]:
        async with self._db.unit_of_work() as session:
            return await session.find(AttendanceLock, {}, sort=[("period_start", -1)], limit=200)

    async def create_lock(
        self, actor: Actor, context: RequestContext, period_start: date, period_end: date, reason: str
    ) -> AttendanceLock:
        if period_end < period_start:
            raise field_error(
                "period_end", "range.reversed", "The end date must be on or after the start date."
            )
        if period_end >= self._authz.today():
            raise field_error(
                "period_end", "attendance.lock_future", "Lock only dates that have already passed."
            )
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "attendance.policy.manage")
            # Active locks never overlap (formerly an exclusion constraint): checked under a lock.
            await session.lock("attendance_locks")
            if await session.exists(
                AttendanceLock,
                {
                    "released_at": None,
                    "period_start": {"$lte": period_end},
                    "period_end": {"$gte": period_start},
                },
            ):
                raise conflict("attendance.lock_overlap", "These dates overlap a lock that is still active.")
            lock = AttendanceLock(
                period_start=period_start, period_end=period_end, reason=reason, locked_by=actor.user_id
            )
            session.add(lock)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.lock.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_lock",
                    target_id=lock.id,
                    changes={
                        "period_start": ValueChange(None, period_start),
                        "period_end": ValueChange(None, period_end),
                    },
                    reason=reason,
                ),
            )
            await session.refresh(lock)
            return lock

    async def release_lock(
        self, actor: Actor, context: RequestContext, lock_id: uuid.UUID, reason: str
    ) -> AttendanceLock:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "attendance.policy.manage")
            lock = await session.get(AttendanceLock, lock_id, for_update=True)
            if lock is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if lock.released_at is not None:
                raise conflict("attendance.lock_released", "This lock was already released.")
            lock.released_at = self._clock.now()
            lock.released_by = actor.user_id
            lock.release_reason = reason
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.lock.released",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_lock",
                    target_id=lock.id,
                    reason=reason,
                ),
            )
            await session.refresh(lock)
            return lock

    # --- setup gaps ----------------------------------------------------------------------------

    async def setup(self, actor: Actor) -> SetupGaps:
        """ATT-12: who in the reader's scope has no shift or no policy today."""
        on = self._authz.today()
        async with self._db.unit_of_work() as session:
            scope = await self._authz.scope_filter(
                session, actor, "attendance.read", people.EMPLOYEE_ID_FIELD
            )
            everyone, _ = await people.employed_colleagues(session, scope, on=on)
            schedule = await Schedule.load(session, [c.id for c in everyone], on, on)
            without_shift, without_policy = [], []
            for colleague in everyone:
                applied = schedule.applied(colleague.id, on)
                if applied is None:
                    without_shift.append(colleague)
                elif applied.policy is None:
                    without_policy.append(colleague)
            shifts = await session.count(Shift, {"status": ShiftStatus.ACTIVE.value})
            effective = {
                "effective_from": {"$lte": on},
                "$or": [{"effective_to": None}, {"effective_to": {"$gt": on}}],
            }
            policies = await session.count(AttendancePolicy, effective)
            organization = await session.count(
                AttendancePolicy, {**effective, "scope_type": PolicyScope.ORGANIZATION.value}
            )
        return SetupGaps(
            on=on,
            shifts=shifts,
            policies=policies,
            employees_in_scope=len(everyone),
            without_shift=without_shift,
            without_policy=without_policy,
            organization_policy=organization > 0,
        )


async def _shift_names(session: Session, shift_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not shift_ids:
        return {}
    return {shift.id: shift.name for shift in await session.find(Shift, {"id": {"$in": list(shift_ids)}})}
