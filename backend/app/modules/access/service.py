"""Roles and role assignments (docs/authorization-model.md §4-6).

Assigning and removing roles needs `role.assign` with step-up, and is audited. Rules applied
regardless of permissions held:

- SOD-3: nobody assigns or removes their own roles (also a database check).
- Derived roles (`employee`, `manager`) are never assigned (also a database check).
- SOD-8: `super_admin` is assigned only with a second super admin's approval: assigning it
  creates a grant request (`grant_requests.py`) and nothing is assigned until it is approved.
- SOD-10: a super admin holds no standing data role (`hr`, `hr_admin`, `payroll_admin`,
  `system_admin`, `auditor`).
- At least two super admins remain (docs/security-architecture.md §9).
- An assignment that came from an elevation or break-glass is ended through its request
  (`revoke`), never removed here, so the request and the assignment stay consistent.

A change to a user's roles ends their sessions' access tokens, so each session refreshes and
gets new tokens (session rotation on role change, docs/security-architecture.md §3.5).
Permissions themselves are resolved per request, so the change applies at once anyway.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.modules.access import repository as repo
from app.modules.access.grant_requests import GrantRequestService
from app.modules.access.models import GrantKind, RoleGrantRequest, UserRole
from app.modules.access.repository import Role
from app.modules.identity import public as identity
from app.modules.org import public as org
from app.platform.audit.records import AuditEvent, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.authz.roles import DATA_ROLES, MIN_SUPER_ADMINS, RoleKey
from app.platform.authz.sod import SodRule, refuse_self
from app.platform.clock import Clock
from app.platform.db import Database, Session, UniqueViolationError
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
        self,
        *,
        database: Database,
        clock: Clock,
        audit: AuditWriter,
        authorizer: Authorizer,
        grant_requests: GrantRequestService,
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer
        self._grant_requests = grant_requests

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
    ) -> Assignment | RoleGrantRequest:
        """A new assignment, or, for `super_admin`, a request a second super admin must approve."""
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
                if request.department_id or request.location_id or request.valid_until:
                    raise _field(
                        "role_key",
                        "role.super_admin_unrestricted",
                        "The super admin role has no department, location or end time.",
                    )
                return await self._grant_requests.request_super_admin(
                    session,
                    actor,
                    context,
                    user_id=user_id,
                    role=role,
                    reason=request.reason,
                    granted=granted,
                )
            held = {key for _, key in await repo.active_assignments(session, user_id, now)}
            record = await self._add(
                session,
                actor=actor,
                context=context,
                permission=granted.key,
                user_id=user_id,
                role=role,
                request=request,
                held=held,
                now=now,
            )
            await identity.rotate_user_sessions(session, user_id, now)
        return Assignment(record, role.key)

    async def assign_many(
        self,
        actor: Actor,
        context: RequestContext,
        user_id: uuid.UUID,
        requests: Sequence[AssignmentRequest],
    ) -> list[Assignment]:
        """Several roles at once (docs/authorization-model.md §5): one transaction, one step-up,
        all or nothing. It only adds: roles already held stay as they are, and a role held
        already, or one not compatible with the others, refuses the whole batch with the
        position of the role at fault. `super_admin` is never part of a batch: it is assigned
        alone, as a request a second super admin approves (SOD-8)."""
        now = self._clock.now()
        if not requests:
            raise _field("roles", "role.batch_empty", "Choose at least one role.")
        for index, request in enumerate(requests):
            if request.valid_until is not None and request.valid_until <= now:
                raise _field(
                    f"roles.{index}.valid_until", "role.valid_until_past", "Choose an end time in the future."
                )
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.assign")
            refuse_self(actor, user_id, SodRule.OWN_ROLES)
            await self._require_user(session, user_id)
            roles: list[Role] = []
            seen: set[tuple[str, uuid.UUID | None, uuid.UUID | None]] = set()
            for index, request in enumerate(requests):
                role = await repo.role_by_key(session, request.role_key)
                if role is None:
                    raise _field(f"roles.{index}.role_key", "role.unknown", "Choose one of the listed roles.")
                if role.is_derived:
                    raise _conflict(
                        "role.derived",
                        "This role is given automatically from employment and reporting lines.",
                    )
                if role.key == RoleKey.SUPER_ADMIN:
                    raise _conflict(
                        "role.super_admin_alone",
                        "Assign the super admin role on its own: it needs another super admin's approval.",
                    )
                slot = (role.key, request.department_id, request.location_id)
                if slot in seen:
                    raise _field(f"roles.{index}.role_key", "role.batch_duplicate", "Choose each role once.")
                seen.add(slot)
                roles.append(role)
            held = {key for _, key in await repo.active_assignments(session, user_id, now)}
            records = [
                await self._add(
                    session,
                    actor=actor,
                    context=context,
                    permission=granted.key,
                    user_id=user_id,
                    role=role,
                    request=request,
                    held=held,
                    now=now,
                )
                for role, request in zip(roles, requests, strict=True)
            ]
            await identity.rotate_user_sessions(session, user_id, now)
        return [Assignment(record, role.key) for record, role in zip(records, roles, strict=True)]

    async def _add(
        self,
        session: Session,
        *,
        actor: Actor,
        context: RequestContext,
        permission: str,
        user_id: uuid.UUID,
        role: Role,
        request: AssignmentRequest,
        held: set[str],
        now: datetime,
    ) -> UserRole:
        """One standing assignment, its checks and its audit row, in the caller's transaction."""
        if RoleKey.SUPER_ADMIN in held and role.key in DATA_ROLES:
            raise _conflict(
                SodRule.SUPER_ADMIN_DATA_ROLE.value,
                "A super admin cannot hold this role permanently. Use a time-bound elevation instead.",
            )
        record = UserRole(
            user_id=user_id,
            role_id=role.id,
            role_key=role.key,
            department_id=request.department_id,
            location_id=request.location_id,
            valid_from=now,
            valid_until=request.valid_until,
            granted_by=actor.user_id,
            grant_reason=request.reason,
        )
        await self._insert(session, record)
        await self._audit.record(
            session,
            AuditEvent(
                action="access.role.assigned",
                actor=actor.audit_actor(),
                context=context,
                permission_used=permission,
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
        return record

    async def _insert(self, session: Session, record: UserRole) -> None:
        # A lapsed assignment of the same role still holds the unique slot until it is closed.
        await repo.close_lapsed(
            session,
            self._clock.now(),
            user_id=record.user_id,
            role_id=record.role_id,
            department_id=record.department_id,
            location_id=record.location_id,
        )
        missing = await org.missing_units(
            session, department_id=record.department_id, location_id=record.location_id
        )
        if "department_id" in missing:
            raise _field("department_id", "org.unknown_department", "Choose an existing department.")
        if "location_id" in missing:
            raise _field("location_id", "org.unknown_location", "Choose an existing location.")
        try:
            repo.add_assignment(session, record)
            await session.flush()
        except UniqueViolationError as exc:
            if exc.index == "uq_user_roles_active_assignment":
                raise _conflict("role.already_assigned", "This person already has this role.") from exc
            raise

    async def remove(
        self, actor: Actor, context: RequestContext, user_id: uuid.UUID, assignment_id: uuid.UUID
    ) -> None:
        """Concurrent removals collide on the holders they lock; each loser runs again and is
        judged on what the winner committed (the assignment gone, or too few super admins left)."""
        now = self._clock.now()
        await self._db.retry_on_conflict(lambda: self._remove(actor, context, user_id, assignment_id, now))

    async def _remove(
        self,
        actor: Actor,
        context: RequestContext,
        user_id: uuid.UUID,
        assignment_id: uuid.UUID,
        now: datetime,
    ) -> None:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.assign")
            refuse_self(actor, user_id, SodRule.OWN_ROLES)
            candidate = await session.get(UserRole, assignment_id)
            if candidate is None or candidate.user_id != user_id:
                raise ProblemError(ProblemType.NOT_FOUND)
            role = await repo.role(session, candidate.role_id)
            if role is None:
                raise RuntimeError("a role assignment references a missing role")
            if candidate.grant_request_id is not None and await repo.request_kind(
                session, candidate.grant_request_id
            ) in (GrantKind.ELEVATION, GrantKind.BREAK_GLASS):
                raise _conflict(
                    "role.temporary_access",
                    "This is temporary access from an access request. End it from the access requests page.",
                )
            if role.key == RoleKey.SUPER_ADMIN:
                # Lock every holder first (in ID order), then count the active accounts that
                # would remain (a disabled super admin cannot act, as when disabling one).
                holders = await repo.lock_role_holders(session, role.id, now)
                remaining = await identity.active_user_ids(
                    session, {holder.user_id for holder in holders if holder.id != assignment_id}
                )
                if len(remaining) < MIN_SUPER_ADMINS:
                    raise _conflict(
                        "role.min_super_admins",
                        f"There must always be at least {MIN_SUPER_ADMINS} active super admins.",
                    )
            record = await repo.lock_assignment(session, assignment_id)
            if record is None or record.revoked_at is not None:
                raise ProblemError(ProblemType.NOT_FOUND)
            record.revoked_at = now
            record.revoked_by = actor.user_id
            if role.key == RoleKey.SUPER_ADMIN:
                await self._grant_requests.end_for_former_super_admin(session, actor, context, user_id)
            await identity.rotate_user_sessions(session, user_id, now)
            await self._audit.record(
                session,
                AuditEvent(
                    action="access.role.removed",
                    actor=actor.audit_actor(),
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
    async def _require_user(session: Session, user_id: uuid.UUID) -> None:
        if await identity.user_status(session, user_id) is None:
            raise ProblemError(ProblemType.NOT_FOUND)
