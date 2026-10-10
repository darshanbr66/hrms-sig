"""Role grant requests: elevation, super admin assignment and break-glass.

docs/authorization-model.md §4.3 and §6, docs/security-architecture.md §9, threat model T6/T6a.

- **Elevation** (`role.elevation.request`): a super admin asks for a role for themselves, with
  a reason, a duration (at most 8 hours) and an optional department or location constraint.
  A *different* super admin approves or rejects it (`role.elevation.approve`) within 24 hours.
  Approval starts it at once: an assignment with exactly the request's window, linked to the
  request. The requester or any other super admin can end it early (`revoke`).
- **Super admin assignment**: `POST /users/{id}/roles` with `super_admin` creates a request
  (SOD-8). A super admin who is neither the assigner nor the assignee approves it (with
  `role.assign` as well), and the assignee must hold no standing data role (SOD-10).
- **Break-glass** (`role.elevation.break_glass`): a super admin self-activates `system_admin`
  for at most 1 hour when nobody can approve. Every other super admin and every auditor is
  emailed at once, and another super admin must acknowledge it afterwards.

Every step needs step-up (all these permissions are SU), the acting account must hold
`super_admin`, and each is audited (with the permission used), recorded as a security event
and emailed to the other super admins. The limits are also CHECK constraints and triggers in
the database (revision 0012), so a bug here cannot grant more.

Permissions are resolved per request, so an elevation stops applying at its end and an
unanswered request cannot be approved after 24 hours, whether or not the expiry job has run;
the job (`access.expire_grants`) only records those transitions and tells the super admins.
Each change to someone's roles ends their access tokens, so their sessions refresh.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import procrastinate
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access import notifications
from app.modules.access import repository as repo
from app.modules.access.models import GrantKind, GrantStatus, Role, RoleGrantRequest, UserRole
from app.modules.identity import public as identity
from app.platform.audit.records import (
    AuditActor,
    AuditEvent,
    RequestContext,
    SecurityEvent,
    SecurityEventType,
    Severity,
    ValueChange,
)
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer, Granted
from app.platform.authz.roles import DATA_ROLES, RoleKey
from app.platform.authz.sod import SodRule
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType

logger = logging.getLogger(__name__)

# Fixed security limits (docs/authorization-model.md §4.3), also CHECK constraints.
MAX_ELEVATION: Final = timedelta(hours=8)
MAX_BREAK_GLASS: Final = timedelta(hours=1)
DECISION_WINDOW: Final = timedelta(hours=24)

TASK_NAME: Final = "access.expire_grants"
SCHEDULE: Final = "* * * * *"
QUEUE: Final = "access"
EXPIRY_BATCH: Final = 100


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def _not_pending() -> ProblemError:
    return _conflict(
        "grant_request.not_pending", "This request was already decided, withdrawn or has expired."
    )


def effective_status(request: RoleGrantRequest, now: datetime) -> GrantStatus:
    """The status as of now: a pending request past its decision window has expired, and an
    active one past its end has ended, even before the expiry job records it."""
    status = GrantStatus(request.status)
    if status is GrantStatus.PENDING and request.requested_at + DECISION_WINDOW <= now:
        return GrantStatus.EXPIRED
    if status is GrantStatus.ACTIVE and request.ends_at is not None and request.ends_at <= now:
        return GrantStatus.ENDED
    return status


@dataclass(frozen=True, slots=True)
class ElevationRequest:
    role_key: str
    reason: str
    duration: timedelta
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class GrantRequestPage:
    items: Sequence[RoleGrantRequest]
    emails: dict[uuid.UUID, str]
    next_cursor: uuid.UUID | None


# Security event and audit action per kind and step.
_EVENTS: Final[dict[tuple[GrantKind, str], SecurityEventType]] = {
    (GrantKind.ELEVATION, "requested"): SecurityEventType.ELEVATION_REQUESTED,
    (GrantKind.ELEVATION, "approved"): SecurityEventType.ELEVATION_APPROVED,
    (GrantKind.ELEVATION, "rejected"): SecurityEventType.ELEVATION_REJECTED,
    (GrantKind.ELEVATION, "revoked"): SecurityEventType.ELEVATION_REVOKED,
    (GrantKind.ELEVATION, "ended"): SecurityEventType.ELEVATION_ENDED,
    (GrantKind.ELEVATION, "expired"): SecurityEventType.ELEVATION_EXPIRED,
    (GrantKind.BREAK_GLASS, "activated"): SecurityEventType.ELEVATION_BREAK_GLASS,
    (GrantKind.BREAK_GLASS, "revoked"): SecurityEventType.ELEVATION_REVOKED,
    (GrantKind.BREAK_GLASS, "ended"): SecurityEventType.ELEVATION_ENDED,
    (GrantKind.BREAK_GLASS, "acknowledged"): SecurityEventType.BREAK_GLASS_ACKNOWLEDGED,
    (GrantKind.SUPER_ADMIN_ASSIGNMENT, "requested"): SecurityEventType.SUPER_ADMIN_REQUESTED,
    (GrantKind.SUPER_ADMIN_ASSIGNMENT, "approved"): SecurityEventType.SUPER_ADMIN_APPROVED,
    (GrantKind.SUPER_ADMIN_ASSIGNMENT, "rejected"): SecurityEventType.SUPER_ADMIN_REJECTED,
    (GrantKind.SUPER_ADMIN_ASSIGNMENT, "revoked"): SecurityEventType.SUPER_ADMIN_REVOKED,
    (GrantKind.SUPER_ADMIN_ASSIGNMENT, "expired"): SecurityEventType.SUPER_ADMIN_EXPIRED,
}
_ACTION_PREFIX: Final = {
    GrantKind.ELEVATION: "access.elevation",
    GrantKind.BREAK_GLASS: "access.break_glass",
    GrantKind.SUPER_ADMIN_ASSIGNMENT: "access.super_admin",
}
_HIGH_SEVERITY: Final = frozenset({SecurityEventType.ELEVATION_BREAK_GLASS})


async def _record(
    session: AsyncSession,
    audit: AuditWriter,
    *,
    request: RoleGrantRequest,
    step: str,
    actor: AuditActor,
    context: RequestContext,
    session_id: uuid.UUID | None,
    granted: Granted | None,
    changes: dict[str, ValueChange] | None = None,
    reason: str | None = None,
) -> None:
    """The audit row and the security event of one step (same transaction as the change)."""
    kind = GrantKind(request.kind)
    event_type = _EVENTS[(kind, step)]
    await audit.record(
        session,
        AuditEvent(
            action=f"{_ACTION_PREFIX[kind]}.{step}",
            actor=actor,
            context=context,
            permission_used=granted.key if granted else None,
            target_type="grant_request",
            target_id=request.id,
            changes=changes,
            reason=reason,
        ),
    )
    by_subject = actor.user_id == request.subject_user_id
    await audit.record_security_event(
        session,
        SecurityEvent(
            event_type,
            Severity.HIGH if event_type in _HIGH_SEVERITY else Severity.WARNING,
            context=context,
            user_id=request.subject_user_id,
            session_id=session_id if by_subject else None,
            details={
                "grant_request_id": request.id,
                "kind": kind.value,
                "role": request.role_key,
                "by_user_id": actor.user_id,
            },
        ),
    )


class GrantRequestService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    # --- reading ------------------------------------------------------------------------------

    async def list(
        self, actor: Actor, *, status: GrantStatus | None, before: uuid.UUID | None, limit: int
    ) -> GrantRequestPage:
        """Every request, newest first. `status` filters on the stored status; pending and
        active rows past their time are reported as expired and ended (`effective_status`)."""
        async with self._db.unit_of_work() as session:
            await self._authz.require(session, actor, "role.read")
            rows, next_cursor = await repo.list_requests(
                session, statuses=[status.value] if status else None, before=before, limit=limit
            )
            emails = await identity.emails_of(session, _people(rows))
        return GrantRequestPage(rows, emails, next_cursor)

    async def emails(self, requests: Sequence[RoleGrantRequest]) -> dict[uuid.UUID, str]:
        """Work emails of the people a request names, for its view."""
        async with self._db.unit_of_work() as session:
            return await identity.emails_of(session, _people(requests))

    async def current_elevation(self, actor: Actor) -> RoleGrantRequest | None:
        """The elevation or break-glass that applies to the actor now, for the elevation banner."""
        if actor.grant_request_id is None:
            return None
        async with self._db.unit_of_work() as session:
            request = await session.get(RoleGrantRequest, actor.grant_request_id)
        if request is None or effective_status(request, self._clock.now()) is not GrantStatus.ACTIVE:
            return None
        return request

    # --- elevation ----------------------------------------------------------------------------

    async def request_elevation(
        self, actor: Actor, context: RequestContext, wanted: ElevationRequest
    ) -> RoleGrantRequest:
        if not timedelta(0) < wanted.duration <= MAX_ELEVATION:
            raise _field("duration_minutes", "grant_request.duration", "Choose from 1 minute to 8 hours.")
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.elevation.request")
            _require_super_admin(actor)
            role = await repo.role_by_key(session, wanted.role_key)
            if role is None or role.is_derived or role.key == RoleKey.SUPER_ADMIN:
                raise _field(
                    "role_key",
                    "grant_request.role_not_elevatable",
                    "Choose a role that can be given for a limited time.",
                )
            request = RoleGrantRequest(
                kind=GrantKind.ELEVATION.value,
                subject_user_id=actor.user_id,
                role_id=role.id,
                role_key=role.key,
                department_id=wanted.department_id,
                location_id=wanted.location_id,
                reason=wanted.reason,
                requested_duration=wanted.duration,
                requested_by=actor.user_id,
                requested_at=now,
                status=GrantStatus.PENDING.value,
                created_at=now,
                updated_at=now,
            )
            await _flush_request(session, request)
            await _record(
                session,
                self._audit,
                request=request,
                step="requested",
                actor=actor.audit_actor(),
                context=context,
                session_id=actor.session_id,
                granted=granted,
                changes={
                    "role": ValueChange(None, role.key),
                    "duration_minutes": ValueChange(None, _minutes(wanted.duration)),
                    "department_id": ValueChange(None, wanted.department_id),
                    "location_id": ValueChange(None, wanted.location_id),
                },
                reason=wanted.reason,
            )
            await notifications.grant_event(session, request, "requested", now, exclude=actor.user_id)
            return request

    # --- super admin assignment (SOD-8) ----------------------------------------------------------

    async def request_super_admin(
        self,
        session: AsyncSession,
        actor: Actor,
        context: RequestContext,
        *,
        user_id: uuid.UUID,
        role: Role,
        reason: str,
        granted: Granted,
    ) -> RoleGrantRequest:
        """Called by role assignment (in its transaction, after `role.assign`, step-up and SOD-3)
        when the role is `super_admin`: nothing is assigned until a second super admin approves."""
        now = self._clock.now()
        await self._check_super_admin_candidate(session, user_id, now)
        request = RoleGrantRequest(
            kind=GrantKind.SUPER_ADMIN_ASSIGNMENT.value,
            subject_user_id=user_id,
            role_id=role.id,
            role_key=role.key,
            reason=reason,
            requested_by=actor.user_id,
            requested_at=now,
            status=GrantStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        await _flush_request(session, request)
        await _record(
            session,
            self._audit,
            request=request,
            step="requested",
            actor=actor.audit_actor(),
            context=context,
            session_id=actor.session_id,
            granted=granted,
            changes={"role": ValueChange(None, role.key)},
            reason=reason,
        )
        await notifications.grant_event(session, request, "requested", now, exclude=actor.user_id)
        return request

    async def _check_super_admin_candidate(
        self, session: AsyncSession, user_id: uuid.UUID, now: datetime
    ) -> None:
        status = await identity.user_status(session, user_id)
        if status is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        if status == "disabled":
            raise _conflict("grant_request.subject_ineligible", "This account is disabled. Enable it first.")
        held = await repo.active_assignments(session, user_id, now)
        if any(key == RoleKey.SUPER_ADMIN for _, key in held):
            raise _conflict("role.already_assigned", "This person already has this role.")
        # SOD-10: a super admin holds no standing data role.
        if any(key in DATA_ROLES and record.grant_request_id is None for record, key in held):
            raise _conflict(
                SodRule.SUPER_ADMIN_DATA_ROLE.value,
                "This person has a role a super admin cannot hold. Remove it first.",
            )

    # --- decisions ------------------------------------------------------------------------------

    async def approve(
        self, actor: Actor, context: RequestContext, request_id: uuid.UUID, note: str | None
    ) -> RoleGrantRequest:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.elevation.approve")
            _require_super_admin(actor)
            request = await self._decidable(session, request_id, actor, now)
            kind = GrantKind(request.kind)
            if kind is GrantKind.SUPER_ADMIN_ASSIGNMENT:
                granted = await self._authz.require(session, actor, "role.assign")
                await self._check_super_admin_candidate(session, request.subject_user_id, now)
            else:
                await self._check_elevation_subject(session, request.subject_user_id, now)
            request.decided_by = actor.user_id
            request.decided_at = now
            request.decision_note = note
            request.updated_at = now
            if kind is GrantKind.SUPER_ADMIN_ASSIGNMENT:
                request.status = GrantStatus.APPROVED.value
                request.starts_at = now
                valid_until = None
            else:
                duration = _required(request.requested_duration)
                request.status = GrantStatus.ACTIVE.value
                request.starts_at = now
                request.ends_at = now + duration
                valid_until = request.ends_at
            await _flush_request(session, request)
            await self._assign(session, request, granted_by=actor.user_id, valid_until=valid_until, now=now)
            await identity.rotate_user_sessions(session, request.subject_user_id, now)
            await _record(
                session,
                self._audit,
                request=request,
                step="approved",
                actor=actor.audit_actor(),
                context=context,
                session_id=actor.session_id,
                granted=granted,
                changes={
                    "status": ValueChange(GrantStatus.PENDING.value, request.status),
                    "ends_at": ValueChange(None, request.ends_at),
                },
                reason=note,
            )
            await notifications.grant_event(session, request, "approved", now, exclude=actor.user_id)
            return request

    async def reject(
        self, actor: Actor, context: RequestContext, request_id: uuid.UUID, note: str | None
    ) -> RoleGrantRequest:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.elevation.approve")
            _require_super_admin(actor)
            request = await self._decidable(session, request_id, actor, now)
            request.status = GrantStatus.REJECTED.value
            request.decided_by = actor.user_id
            request.decided_at = now
            request.decision_note = note
            request.updated_at = now
            await _record(
                session,
                self._audit,
                request=request,
                step="rejected",
                actor=actor.audit_actor(),
                context=context,
                session_id=actor.session_id,
                granted=granted,
                changes={"status": ValueChange(GrantStatus.PENDING.value, request.status)},
                reason=note,
            )
            await notifications.grant_event(session, request, "rejected", now, exclude=actor.user_id)
            return request

    async def _decidable(
        self, session: AsyncSession, request_id: uuid.UUID, actor: Actor, now: datetime
    ) -> RoleGrantRequest:
        request = await repo.lock_request(session, request_id)
        if request is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        # SOD-8 and SOD-9: nobody decides a request they made or that would give them a role.
        if actor.user_id in (request.requested_by, request.subject_user_id):
            raise AccessDenied(code=SodRule.OWN_REQUEST.value)
        if effective_status(request, now) is not GrantStatus.PENDING:
            raise _not_pending()
        return request

    async def _check_elevation_subject(
        self, session: AsyncSession, user_id: uuid.UUID, now: datetime
    ) -> None:
        """An elevation goes only to an active account that is still a super admin."""
        status = await identity.user_status(session, user_id)
        held = {key for _, key in await repo.active_assignments(session, user_id, now)}
        if status != "active" or RoleKey.SUPER_ADMIN not in held:
            raise _conflict(
                "grant_request.subject_ineligible",
                "The person who asked is no longer an active super admin, so this cannot be approved.",
            )

    async def _assign(
        self,
        session: AsyncSession,
        request: RoleGrantRequest,
        *,
        granted_by: uuid.UUID,
        valid_until: datetime | None,
        now: datetime,
    ) -> None:
        """The assignment a request grants, exactly as the request says (a database trigger
        checks the match). A lapsed assignment of the same role is closed first."""
        await repo.close_lapsed(
            session,
            now,
            user_id=request.subject_user_id,
            role_id=request.role_id,
            department_id=request.department_id,
            location_id=request.location_id,
        )
        record = UserRole(
            user_id=request.subject_user_id,
            role_id=request.role_id,
            department_id=request.department_id,
            location_id=request.location_id,
            valid_from=_required(request.starts_at),
            valid_until=valid_until,
            granted_by=granted_by,
            grant_reason=request.reason,
            grant_request_id=request.id,
        )
        try:
            async with session.begin_nested():
                session.add(record)
                await session.flush()
        except IntegrityError as exc:
            if _constraint(exc) == "uq_user_roles_active_assignment":
                raise _conflict("role.already_assigned", "This person already has this role.") from exc
            raise

    # --- ending early ---------------------------------------------------------------------------

    async def revoke(self, actor: Actor, context: RequestContext, request_id: uuid.UUID) -> RoleGrantRequest:
        """Withdraw a pending request, or end an active elevation or break-glass early. Any
        super admin may, including its requester. An approved super admin assignment is
        removed as a role assignment instead (with the two-super-admin minimum)."""
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.elevation.approve")
            _require_super_admin(actor)
            request = await repo.lock_request(session, request_id)
            if request is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            before = effective_status(request, now)
            if before not in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                raise _conflict(
                    "grant_request.not_revocable",
                    "This request is not pending or active, so it cannot be ended.",
                )
            request.status = GrantStatus.REVOKED.value
            request.revoked_by = actor.user_id
            request.revoked_at = now
            request.updated_at = now
            if before is GrantStatus.ACTIVE:
                assignment = await repo.lock_request_assignment(session, request.id)
                if assignment is not None and assignment.revoked_at is None:
                    assignment.revoked_at = now
                    assignment.revoked_by = actor.user_id
                await identity.rotate_user_sessions(session, request.subject_user_id, now)
            await _record(
                session,
                self._audit,
                request=request,
                step="revoked",
                actor=actor.audit_actor(),
                context=context,
                session_id=actor.session_id,
                granted=granted,
                changes={"status": ValueChange(before.value, request.status)},
            )
            await notifications.grant_event(session, request, "revoked", now, exclude=actor.user_id)
            return request

    # --- break-glass ----------------------------------------------------------------------------

    async def break_glass(
        self, actor: Actor, context: RequestContext, reason: str, duration: timedelta
    ) -> RoleGrantRequest:
        if not timedelta(0) < duration <= MAX_BREAK_GLASS:
            raise _field("duration_minutes", "grant_request.duration", "Choose from 1 to 60 minutes.")
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.elevation.break_glass")
            _require_super_admin(actor)
            role = _required(await repo.role_by_key(session, RoleKey.SYSTEM_ADMIN.value))
            request = RoleGrantRequest(
                kind=GrantKind.BREAK_GLASS.value,
                subject_user_id=actor.user_id,
                role_id=role.id,
                role_key=role.key,
                reason=reason,
                requested_duration=duration,
                requested_by=actor.user_id,
                requested_at=now,
                status=GrantStatus.ACTIVE.value,
                starts_at=now,
                ends_at=now + duration,
                created_at=now,
                updated_at=now,
            )
            await _flush_request(session, request)
            await self._assign(
                session, request, granted_by=actor.user_id, valid_until=request.ends_at, now=now
            )
            await identity.rotate_user_sessions(session, actor.user_id, now)
            await _record(
                session,
                self._audit,
                request=request,
                step="activated",
                actor=actor.audit_actor(),
                context=context,
                session_id=actor.session_id,
                granted=granted,
                changes={
                    "role": ValueChange(None, role.key),
                    "ends_at": ValueChange(None, request.ends_at),
                },
                reason=reason,
            )
            await notifications.grant_event(
                session, request, "break_glass", now, exclude=actor.user_id, include_auditors=True
            )
            return request

    async def end_for_former_super_admin(
        self, session: AsyncSession, actor: Actor, context: RequestContext, user_id: uuid.UUID
    ) -> None:
        """Called when someone's `super_admin` role is removed (in that transaction): their
        elevation or break-glass ends now and their pending requests are withdrawn, because
        temporary access is only ever for a current super admin."""
        now = self._clock.now()
        for request in await repo.lock_open_requests_of(session, user_id):
            before = effective_status(request, now)
            if before not in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                continue
            request.status = GrantStatus.REVOKED.value
            request.revoked_by = actor.user_id
            request.revoked_at = now
            request.updated_at = now
            if before is GrantStatus.ACTIVE:
                assignment = await repo.lock_request_assignment(session, request.id)
                if assignment is not None and assignment.revoked_at is None:
                    assignment.revoked_at = now
                    assignment.revoked_by = actor.user_id
            await _record(
                session,
                self._audit,
                request=request,
                step="revoked",
                actor=actor.audit_actor(),
                context=context,
                session_id=actor.session_id,
                granted=None,
                changes={"status": ValueChange(before.value, request.status)},
                reason="The person is no longer a super admin",
            )
            await notifications.grant_event(session, request, "revoked", now, exclude=actor.user_id)

    async def acknowledge(
        self, actor: Actor, context: RequestContext, request_id: uuid.UUID
    ) -> RoleGrantRequest:
        """The post-incident review of a break-glass, by a super admin other than its user."""
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "role.elevation.approve")
            _require_super_admin(actor)
            request = await repo.lock_request(session, request_id)
            if request is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if request.kind != GrantKind.BREAK_GLASS:
                raise _conflict("grant_request.not_break_glass", "Only a break-glass use is acknowledged.")
            if actor.user_id == request.requested_by:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            # Post-incident: what was done under it is known only once it no longer applies.
            if effective_status(request, now) is GrantStatus.ACTIVE:
                raise _conflict(
                    "grant_request.still_active",
                    "This break-glass access is still active. Review it after it ends, or end it now.",
                )
            if request.acknowledged_at is not None:
                raise _conflict(
                    "grant_request.already_acknowledged", "This break-glass use was already reviewed."
                )
            request.acknowledged_by = actor.user_id
            request.acknowledged_at = now
            request.updated_at = now
            await _record(
                session,
                self._audit,
                request=request,
                step="acknowledged",
                actor=actor.audit_actor(),
                context=context,
                session_id=actor.session_id,
                granted=granted,
            )
            return request


# --- expiry (worker) ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpiryResult:
    expired: int = 0
    ended: int = 0
    lapsed_assignments: int = 0


async def expire_due(database: Database, clock: Clock, audit: AuditWriter) -> ExpiryResult:
    """Record what time has already decided: pending requests past the decision window expire,
    active elevations and break-glass past their end end (their assignment is closed and the
    person's sessions refresh), and other lapsed assignments are closed. Each transition is
    audited as a job and emailed to the super admins."""
    now = clock.now()
    expired = ended = 0
    async with database.unit_of_work() as session:
        rows = await repo.lock_due_requests(
            session, now=now, requested_before=now - DECISION_WINDOW, limit=EXPIRY_BATCH
        )
        for request in rows:
            before = request.status
            if before == GrantStatus.PENDING:
                request.status = GrantStatus.EXPIRED.value
                step = "expired"
                expired += 1
            else:
                request.status = GrantStatus.ENDED.value
                step = "ended"
                ended += 1
                assignment = await repo.lock_request_assignment(session, request.id)
                if assignment is not None and assignment.revoked_at is None:
                    assignment.revoked_at = assignment.valid_until
                await identity.rotate_user_sessions(session, request.subject_user_id, now)
            request.updated_at = now
            await _record(
                session,
                audit,
                request=request,
                step=step,
                actor=AuditActor.job(),
                context=RequestContext(),
                session_id=None,
                granted=None,
                changes={"status": ValueChange(before, request.status)},
            )
            await notifications.grant_event(session, request, step, now, exclude=None)
        await session.flush()
        lapsed = await repo.close_lapsed(session, now)
    return ExpiryResult(expired, ended, len(lapsed))


def register_grant_expiry(
    job_app: procrastinate.App, database: Database, clock: Clock, audit: AuditWriter
) -> None:
    """Every minute, one run at a time (`api-architecture.md` §10, `access.expire_grants`)."""

    @job_app.periodic(cron=SCHEDULE, periodic_id=TASK_NAME)
    @job_app.task(name=TASK_NAME, queue=QUEUE, lock=TASK_NAME)
    async def expire_grants(timestamp: int) -> None:
        result = await expire_due(database, clock, audit)
        if result != ExpiryResult():
            logger.info(
                "access.grants_expired",
                extra={
                    "expired": result.expired,
                    "ended": result.ended,
                    "lapsed_assignments": result.lapsed_assignments,
                },
            )


# --- helpers ------------------------------------------------------------------------------------


def _people(requests: Sequence[RoleGrantRequest]) -> set[uuid.UUID]:
    return {
        user_id
        for row in requests
        for user_id in (
            row.subject_user_id,
            row.requested_by,
            row.decided_by,
            row.revoked_by,
            row.acknowledged_by,
        )
        if user_id is not None
    }


def _require_super_admin(actor: Actor) -> None:
    """The grant-request workflow belongs to super admins (SOD-8, SOD-9), whatever else grants
    its permissions."""
    if RoleKey.SUPER_ADMIN not in actor.role_keys:
        raise AccessDenied(code="grant_request.super_admin_only")


async def _flush_request(session: AsyncSession, request: RoleGrantRequest) -> None:
    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError as exc:
        constraint = _constraint(exc)
        if constraint == "uq_role_grant_requests_pending":
            raise _conflict(
                "grant_request.pending_exists",
                "There is already a pending request of this kind for this person. "
                "Wait for its decision or withdraw it.",
            ) from exc
        if constraint == "uq_role_grant_requests_active":
            raise _conflict(
                "grant_request.already_elevated",
                "This person already has temporary access. End it before starting another.",
            ) from exc
        if constraint == "fk_role_grant_requests_department_id":
            raise _field("department_id", "org.unknown_department", "Choose an existing department.") from exc
        if constraint == "fk_role_grant_requests_location_id":
            raise _field("location_id", "org.unknown_location", "Choose an existing location.") from exc
        raise


def _constraint(exc: IntegrityError) -> str | None:
    name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    return name if isinstance(name, str) else None


def _minutes(duration: timedelta) -> int:
    return int(duration.total_seconds() // 60)


def _required[T](value: T | None) -> T:
    if value is None:
        raise RuntimeError("an expected grant request value is missing")
    return value
