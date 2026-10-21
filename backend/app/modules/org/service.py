"""Locations, departments and designations (docs/database-design.md §4.3, product-requirements
EMP-2).

- Reading needs `org.read`; creating and changing needs `org.manage`, and every change is
  audited with its old and new values (organization records hold no personal data).
- Records are never deleted: they are archived, which keeps them on existing job history
  and hides them from new placements.
- Changes carry the version they were based on (If-Match); a stale one is refused.
- A record is archived only when nothing still depends on it: no employee who has not left
  has a current or future job row in it, and (for a department) no active department sits
  inside it. A parent department must be active.
- A department head must be an existing employee who has not left (no last working day
  before today).
- A location's time zone must be an IANA zone name. A department cannot become its own
  ancestor. No organization record is seeded: Sigvitas enters its own.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Final
from zoneinfo import available_timezones

from app.modules.org.models import Department, Designation, Location, OrgUnitStatus
from app.modules.people import public as people
from app.platform.audit.records import AuditEvent, RequestContext, change_of
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock
from app.platform.concurrency import check_version
from app.platform.db import CASE_INSENSITIVE, Database, Session, UniqueViolationError
from app.platform.errors import ProblemError, ProblemType

type OrgRecord = Location | Department | Designation

_TIME_ZONES: Final = frozenset(available_timezones())
_KINDS: Final[dict[type[Any], str]] = {
    Location: "location",
    Department: "department",
    Designation: "designation",
}
_JOB_COLUMNS: Final[dict[type[Any], str]] = {
    Location: "location_id",
    Department: "department_id",
    Designation: "designation_id",
}
# Departments nest; a deeper chain than this is refused rather than walked for ever.
MAX_DEPARTMENT_DEPTH: Final = 20


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


class OrgService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    # --- reads ---------------------------------------------------------------------------------

    async def records(self, model: type[OrgRecord], *, include_archived: bool) -> list[Any]:
        query = {} if include_archived else {"status": OrgUnitStatus.ACTIVE.value}
        async with self._db.unit_of_work() as session:
            return await session.find(model, query, sort=[("name", 1), ("id", 1)], collation=CASE_INSENSITIVE)

    async def head_names(self, departments: Sequence[Department]) -> dict[uuid.UUID, str]:
        heads = {d.head_employee_id for d in departments if d.head_employee_id is not None}
        if not heads:
            return {}
        async with self._db.unit_of_work() as session:
            return await people.employee_names(session, heads)

    async def get(self, model: type[OrgRecord], record_id: uuid.UUID) -> Any:
        async with self._db.unit_of_work() as session:
            record = await session.get(model, record_id)
        if record is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        return record

    # --- writes --------------------------------------------------------------------------------

    async def create(
        self, actor: Actor, context: RequestContext, model: type[OrgRecord], values: Mapping[str, Any]
    ) -> Any:
        if model is Location:
            _check_time_zone(values["time_zone"])
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "org.manage")
            if model is Department and values.get("parent_id") is not None:
                await _check_parent(session, None, values["parent_id"])
            if model is Department and values.get("head_employee_id") is not None:
                await _check_head(session, values["head_employee_id"], self._authz.today())
            record: Any = model(**values)
            session.add(record)
            await _flush_unique(session)
            kind = _KINDS[model]
            await self._audit.record(
                session,
                AuditEvent(
                    action=f"org.{kind}.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type=kind,
                    target_id=record.id,
                    changes={name: change_of(name, None, value) for name, value in values.items()},
                ),
            )
            await session.refresh(record)
        return record

    async def update(
        self,
        actor: Actor,
        context: RequestContext,
        model: type[OrgRecord],
        record_id: uuid.UUID,
        *,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> Any:
        if model is Location and values.get("time_zone") is not None:
            _check_time_zone(values["time_zone"])
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "org.manage")
            record: Any = await session.get(model, record_id, for_update=True)
            if record is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            check_version(record.version, expected_version)
            for required in ("name", "status", "time_zone", "country_code"):
                if required in values and values[required] is None:
                    raise field_error(required, "org.required", "Enter a value.")
            if values.get("status") == OrgUnitStatus.ARCHIVED.value and record.status != values["status"]:
                await self._check_unused(session, record)
            if isinstance(record, Department):
                if values.get("parent_id") is not None:
                    await _check_parent(session, record.id, values["parent_id"])
                if values.get("head_employee_id") is not None:
                    await _check_head(session, values["head_employee_id"], self._authz.today())
            changes = {
                name: change_of(name, getattr(record, name), value)
                for name, value in values.items()
                if getattr(record, name) != value
            }
            if changes:
                for name, value in values.items():
                    setattr(record, name, value)
                record.version += 1
                record.updated_at = self._clock.now()
                await _flush_unique(session)
                kind = _KINDS[model]
                await self._audit.record(
                    session,
                    AuditEvent(
                        action=f"org.{kind}.updated",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=granted.key,
                        target_type=kind,
                        target_id=record.id,
                        changes=changes,
                    ),
                )
        return record

    async def _check_unused(self, session: Session, record: OrgRecord) -> None:
        kind = _KINDS[type(record)]
        placed = await people.placed_in_unit_count(
            session, _JOB_COLUMNS[type(record)], record.id, self._authz.today()
        )
        if placed:
            noun = "employee is" if placed == 1 else "employees are"
            raise _conflict(
                "org.in_use",
                f"{placed} {noun} still placed in this {kind}. "
                "Record a job change for them first, then archive it.",
            )
        if isinstance(record, Department):
            children = await session.count(
                Department, {"parent_id": record.id, "status": OrgUnitStatus.ACTIVE.value}
            )
            if children:
                raise _conflict(
                    "department.has_active_children",
                    "Active departments sit inside this one. Move or archive them first.",
                )


async def _check_head(session: Session, employee_id: uuid.UUID, today: date) -> None:
    exists, left_on = await people.exit_date(session, employee_id)
    if not exists:
        raise field_error("head_employee_id", "org.unknown_employee", "Choose an existing employee.")
    if left_on is not None and left_on < today:
        raise field_error(
            "head_employee_id", "department.head_left", "This person has left. Choose a current employee."
        )


def _check_time_zone(value: str) -> None:
    if value not in _TIME_ZONES:
        raise field_error(
            "time_zone", "location.time_zone_unknown", "Choose a time zone such as Asia/Kolkata."
        )


async def _check_parent(session: Session, department_id: uuid.UUID | None, parent_id: uuid.UUID) -> None:
    """The parent exists and is not the department itself or one of its descendants."""
    current: uuid.UUID | None = parent_id
    for _ in range(MAX_DEPARTMENT_DEPTH):
        if current is None:
            return
        if current == department_id:
            raise field_error(
                "parent_id",
                "department.cycle",
                "A department cannot sit inside itself or one of its own teams.",
            )
        parent = await session.get(Department, current)
        if parent is None:
            raise field_error("parent_id", "department.unknown_parent", "Choose an existing department.")
        if current == parent_id and parent.status != OrgUnitStatus.ACTIVE.value:
            raise field_error(
                "parent_id", "department.parent_archived", "Choose an active department as the parent."
            )
        current = parent.parent_id
    raise field_error("parent_id", "department.too_deep", "This department structure is too deep.")


async def _flush_unique(session: Session) -> None:
    try:
        await session.flush()
    except UniqueViolationError as exc:
        if exc.index.startswith("uq_") and exc.index.endswith("_code"):
            raise _conflict("org.code_in_use", "Another record already uses this code.") from exc
        raise
