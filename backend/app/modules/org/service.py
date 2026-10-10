"""Locations, departments and designations (docs/database-design.md §4.3, product-requirements
EMP-2).

- Reading needs `org.read`; creating and changing needs `org.manage`, and every change is
  audited with its old and new values (organization records hold no personal data).
- Records are never deleted: they are archived, which keeps them on existing job history
  and hides them from new placements.
- Changes carry the version they were based on (If-Match); a stale one is refused.
- A location's time zone must be an IANA zone name. A department cannot become its own
  ancestor. No organization record is seeded: Sigvitas enters its own.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final
from zoneinfo import available_timezones

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.org.models import Department, Designation, Location, OrgUnitStatus
from app.modules.people import public as people
from app.platform.audit.records import AuditEvent, RequestContext, change_of
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock
from app.platform.concurrency import check_version
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType

type OrgRecord = Location | Department | Designation

_TIME_ZONES: Final = frozenset(available_timezones())
_KINDS: Final[dict[type[Any], str]] = {
    Location: "location",
    Department: "department",
    Designation: "designation",
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
        query = select(model).order_by(func.lower(model.name), model.id)
        if not include_archived:
            query = query.where(model.status == OrgUnitStatus.ACTIVE.value)
        async with self._db.unit_of_work() as session:
            return list(await session.scalars(query))

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
            record: Any = await session.get(model, record_id, with_for_update=True, populate_existing=True)
            if record is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            check_version(record.version, expected_version)
            for required in ("name", "status", "time_zone", "country_code"):
                if required in values and values[required] is None:
                    raise field_error(required, "org.required", "Enter a value.")
            if isinstance(record, Department):
                if values.get("parent_id") is not None:
                    await _check_parent(session, record.id, values["parent_id"])
                if values.get("head_employee_id") is not None and not await people.employee_exists(
                    session, values["head_employee_id"]
                ):
                    raise field_error(
                        "head_employee_id", "org.unknown_employee", "Choose an existing employee."
                    )
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


def _check_time_zone(value: str) -> None:
    if value not in _TIME_ZONES:
        raise field_error(
            "time_zone", "location.time_zone_unknown", "Choose a time zone such as Asia/Kolkata."
        )


async def _check_parent(session: AsyncSession, department_id: uuid.UUID | None, parent_id: uuid.UUID) -> None:
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
        current = parent.parent_id
    raise field_error("parent_id", "department.too_deep", "This department structure is too deep.")


async def _flush_unique(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        if "uq_" in str(exc.orig) and "_code" in str(exc.orig):
            raise _conflict("org.code_in_use", "Another record already uses this code.") from exc
        raise
