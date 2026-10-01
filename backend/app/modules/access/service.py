"""Roles and role assignments (docs/authorization-model.md §4-6).

Assigning and removing roles needs `role.assign` with step-up, and is audited. Rules applied
regardless of permissions held:

- SOD-3: nobody assigns or removes their own roles (also a database check).
- Derived roles (`employee`, `manager`) are never assigned (also a database check).
- SOD-8: `super_admin` is assigned only with a second super admin's approval. That approval
  workflow arrives with grant requests; until then assigning it is refused, and the only
  super admins are the two the bootstrap command creates.
- SOD-10: a super admin holds no standing data role (`hr`, `hr_admin`, `payroll_admin`,
  `system_admin`, `auditor`).
- At least two super admins remain (docs/security-architecture.md §9).

A change to a user's roles ends their sessions' access tokens, so each session refreshes and
gets new tokens (session rotation on role change, docs/security-architecture.md §3.5).
Permissions themselves are resolved per request, so the change applies at once anyway.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access import repository as repo
from app.modules.access.models import Role, UserRole
from app.modules.identity import public as identity
from app.platform.audit.records import AuditActor, AuditEvent, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.authz.roles import DATA_ROLES, MIN_SUPER_ADMINS, RoleKey
from app.platform.authz.sod import SodRule, refuse_self
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType


@dataclass(frozen=True, slots=True)
class RoleWithPermissions:
    role: Role
    permission_keys: Sequence[str]


@dataclass(frozen=True, slots=True)
class Assignment:
    record: UserRole
    role_key: str


@dataclass(frozen=True, slots=True)
class AssignmentRequest:
    role_key: str
    reason: str
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    valid_until: datetime | None = None


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


class AccessService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    async def list_roles(self) -> list[RoleWithPermissions]:
        async with self._db.unit_of_work() as session:
            roles = await repo.roles(session)
            keys = await repo.role_permission_keys(session, [role.id for role in roles])
        return [RoleWithPermissions(role, keys[role.id]) for role in roles]

    async def get_role(self, role_id: uuid.UUID) -> RoleWithPermissions:
        async with self._db.unit_of_work() as session:
            role = await repo.role(session, role_id)
            if role is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            keys = await repo.role_permission_keys(session, [role.id])
        return RoleWithPermissions(role, keys[role.id])

    async def assignments_of(self, user_id: uuid.UUID) -> list[Assignment]:
        async with self._db.unit_of_work() as session:
            await self._require_user(session, user_id)
            rows = await repo.active_assignments(session, user_id, self._clock.now())
        return [Assignment(record, key) for record, key in rows]

    async def assign(
        self, actor: Actor, context: RequestContext, user_id: uuid.UUID, request: AssignmentRequest
    ) -> Assignment:
        now = self._clock.now()
        if request.valid_until is not None and request.valid_until <= now:
            raise _field("valid_until", "role.valid_until_past", "Choose an end time in the future.")
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.assign")
            refuse_self(actor, user_id, SodRule.OWN_ROLES)
            await self._require_user(session, user_id)
            role = await repo.role_by_key(session, request.role_key)
            if role is None:
                raise _field("role_key", "role.unknown", "Choose one of the listed roles.")
            if role.is_derived:
                raise _conflict(
                    "role.derived", "This role is given automatically from employment and reporting lines."
                )
            if role.key == RoleKey.SUPER_ADMIN:
                raise _conflict(
                    SodRule.SUPER_ADMIN_ASSIGNMENT.value,
                    "Assigning the super admin role needs approval by a second super admin.",
                )
            held = {key for _, key in await repo.active_assignments(session, user_id, now)}
            if RoleKey.SUPER_ADMIN in held and role.key in DATA_ROLES:
                raise _conflict(
                    SodRule.SUPER_ADMIN_DATA_ROLE.value,
                    "A super admin cannot hold this role permanently. Use a time-bound elevation instead.",
                )
            record = UserRole(
                user_id=user_id,
                role_id=role.id,
                department_id=request.department_id,
                location_id=request.location_id,
                valid_from=now,
                valid_until=request.valid_until,
                granted_by=actor.user_id,
                grant_reason=request.reason,
            )
            await self._insert(session, record)
            await identity.rotate_user_sessions(session, user_id, now)
            await self._audit.record(
                session,
                AuditEvent(
                    action="access.role.assigned",
                    actor=AuditActor.user(actor.user_id, session_id=actor.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="user",
                    target_id=user_id,
                    changes={
                        "role": ValueChange(None, role.key),
                        "assignment_id": ValueChange(None, record.id),
                        "department_id": ValueChange(None, request.department_id),
                        "location_id": ValueChange(None, request.location_id),
                        "valid_until": ValueChange(None, request.valid_until),
                    },
                    reason=request.reason,
                ),
            )
        return Assignment(record, role.key)

    async def _insert(self, session: AsyncSession, record: UserRole) -> None:
        try:
            async with session.begin_nested():
                session.add(record)
                await session.flush()
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_user_roles_active_assignment":
                raise _conflict("role.already_assigned", "This person already has this role.") from exc
            if constraint == "fk_user_roles_department_id":
                raise _field(
                    "department_id", "org.unknown_department", "Choose an existing department."
                ) from exc
            if constraint == "fk_user_roles_location_id":
                raise _field("location_id", "org.unknown_location", "Choose an existing location.") from exc
            raise

    async def remove(
        self, actor: Actor, context: RequestContext, user_id: uuid.UUID, assignment_id: uuid.UUID
    ) -> None:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.assign")
            refuse_self(actor, user_id, SodRule.OWN_ROLES)
            candidate = await session.get(UserRole, assignment_id)
            if candidate is None or candidate.user_id != user_id:
                raise ProblemError(ProblemType.NOT_FOUND)
            role = await repo.role(session, candidate.role_id)
            if role is None:
                raise RuntimeError("a role assignment references a missing role")
            if role.key == RoleKey.SUPER_ADMIN:
                # Lock every holder first (in ID order), then count who would remain.
                holders = await repo.lock_role_holders(session, role.id, now)
                remaining = {holder.user_id for holder in holders if holder.id != assignment_id}
                if len(remaining) < MIN_SUPER_ADMINS:
                    raise _conflict(
                        "role.min_super_admins",
                        f"There must always be at least {MIN_SUPER_ADMINS} super admins.",
                    )
            record = await repo.lock_assignment(session, assignment_id)
            if record is None or record.revoked_at is not None:
                raise ProblemError(ProblemType.NOT_FOUND)
            record.revoked_at = now
            record.revoked_by = actor.user_id
            await identity.rotate_user_sessions(session, user_id, now)
            await self._audit.record(
                session,
                AuditEvent(
                    action="access.role.removed",
                    actor=AuditActor.user(actor.user_id, session_id=actor.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="user",
                    target_id=user_id,
                    changes={
                        "role": ValueChange(role.key, None),
                        "assignment_id": ValueChange(record.id, None),
                    },
                ),
            )

    @staticmethod
    async def _require_user(session: AsyncSession, user_id: uuid.UUID) -> None:
        if await identity.user_status(session, user_id) is None:
            raise ProblemError(ProblemType.NOT_FOUND)
