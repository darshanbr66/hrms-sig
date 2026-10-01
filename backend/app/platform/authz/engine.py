"""The authorization engine (docs/authorization-model.md §7, docs/security-architecture.md §4).

- `require` decides one action, optionally on one subject employee: it finds the actor's
  grants for the permission, checks whether the subject falls in a held scope (`all` within
  the assignment's department/location constraint, `team` through the reporting chain on the
  relevant date, `self` by identity), then checks step-up. It returns the permission it
  relied on, so the caller can record it in the audit row.
- `scope_filter` turns the scopes the actor holds into one SQL predicate on an employee ID
  column, for list queries. With no scope held it refuses before any query runs.
- Denial is the default: an unknown permission, an absent grant or an unmatched subject is
  refused.

The engine does not import business modules. Reporting lines and job placement come through
the `Relationships` protocol, which the people module implements and the application wires in.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, Protocol

from sqlalchemy import ColumnElement, Select, false, or_, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from app.platform.authz.catalog import PERMISSIONS, Permission, Scope, scoped_keys
from app.platform.authz.context import Actor, Constraint
from app.platform.clock import Clock
from app.platform.errors import ProblemError, ProblemType

# docs/security-architecture.md §3.6.
STEP_UP_WINDOW: Final = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Placement:
    """Where an employee works on a date: their job row's department and location."""

    department_id: uuid.UUID
    location_id: uuid.UUID


class Relationships(Protocol):
    async def is_team_member(
        self, session: AsyncSession, manager_employee_id: uuid.UUID, employee_id: uuid.UUID, on: date
    ) -> bool: ...

    def team_member_ids_query(self, manager_employee_id: uuid.UUID, on: date) -> Select[uuid.UUID]: ...

    async def placement(
        self, session: AsyncSession, employee_id: uuid.UUID, on: date
    ) -> Placement | None: ...

    def placed_employee_ids_query(
        self, department_id: uuid.UUID | None, location_id: uuid.UUID | None, on: date
    ) -> Select[uuid.UUID]: ...


class AccessDenied(ProblemError):
    """403, or 404 where confirming that the resource exists would itself be a disclosure."""

    def __init__(self, *, hide_existence: bool = False, code: str | None = None) -> None:
        super().__init__(
            ProblemType.NOT_FOUND if hide_existence else ProblemType.FORBIDDEN,
            extensions={"code": code} if code and not hide_existence else None,
        )
        self.code = code


class StepUpRequired(ProblemError):
    def __init__(self) -> None:
        super().__init__(
            ProblemType.STEP_UP_REQUIRED, extensions={"max_age_seconds": int(STEP_UP_WINDOW.total_seconds())}
        )


class MfaEnrolmentRequired(ProblemError):
    def __init__(self) -> None:
        super().__init__(ProblemType.MFA_ENROLMENT_REQUIRED)


@dataclass(frozen=True, slots=True)
class Granted:
    """The permission an allowed decision relied on."""

    permission: Permission

    @property
    def key(self) -> str:
        return self.permission.key

    @property
    def audit_reads(self) -> bool:
        return self.permission.audit_reads


def has_recent_step_up(actor: Actor, now: datetime) -> bool:
    return actor.step_up_at is not None and now - actor.step_up_at <= STEP_UP_WINDOW


class Authorizer:
    def __init__(self, relationships: Relationships, clock: Clock) -> None:
        self._relationships = relationships
        self._clock = clock

    def today(self) -> date:
        """The date reporting lines and placements are resolved on for current access.

        Resolved in UTC (a documented M1 limitation: per-location dates arrive with M2's
        location time zones).
        """
        return self._clock.now().date()

    # --- capability -----------------------------------------------------------------

    @staticmethod
    def candidate_keys(base: str) -> tuple[str, ...]:
        """Catalog keys that satisfy `base`: the exact key, or its scoped variants."""
        if base in PERMISSIONS:
            return (base,)
        keys = scoped_keys(base)
        if not keys:
            raise ValueError(f"unknown permission {base!r}")
        return keys

    def holds_any(self, actor: Actor, base: str) -> bool:
        return any(actor.holds(key) for key in self.candidate_keys(base))

    def require_step_up(self, actor: Actor) -> None:
        if not has_recent_step_up(actor, self._clock.now()):
            raise StepUpRequired

    # --- single decision -------------------------------------------------------------

    async def require(
        self,
        session: AsyncSession,
        actor: Actor,
        base: str,
        *,
        subject_employee_id: uuid.UUID | None = None,
        on: date | None = None,
        hide_existence: bool = False,
    ) -> Granted:
        """Allow or refuse one action. `on` is the date the subject relationship is judged
        on: today for current data, the record's own date for historical records."""
        keys = self.candidate_keys(base)
        matched: Permission | None = None
        if subject_employee_id is None:
            # Not about one employee's data: only an exact catalog key can be decided, for
            # example `user.disable` or the account-level `auth.session.revoke.all`.
            if base in PERMISSIONS and actor.holds(base):
                matched = PERMISSIONS[base]
        else:
            matched = await self._match_subject(session, actor, keys, subject_employee_id, on or self.today())
        if matched is None:
            raise AccessDenied(hide_existence=hide_existence)
        if matched.step_up:
            self.require_step_up(actor)
        return Granted(matched)

    async def _match_subject(
        self, session: AsyncSession, actor: Actor, keys: Sequence[str], subject: uuid.UUID, on: date
    ) -> Permission | None:
        held = [PERMISSIONS[key] for key in keys if actor.holds(key)]
        # Narrowest scope first, so the audit row names the least permission that sufficed.
        for permission in sorted(
            held, key=lambda p: (Scope.SELF, Scope.TEAM, Scope.ALL).index(p.scope or Scope.ALL)
        ):
            if await self._subject_in_scope(session, actor, permission, subject, on):
                return permission
        return None

    async def _subject_in_scope(
        self, session: AsyncSession, actor: Actor, permission: Permission, subject: uuid.UUID, on: date
    ) -> bool:
        match permission.scope:
            case Scope.SELF:
                return actor.employee_id is not None and actor.employee_id == subject
            case Scope.TEAM:
                return actor.employee_id is not None and await self._relationships.is_team_member(
                    session, actor.employee_id, subject, on
                )
            case Scope.ALL | None:
                # An unscoped permission decided for a subject (`employee.lifecycle.manage`,
                # `employee.create`) reaches every employee, like `all`, and the role
                # assignment's department or location constraint narrows it the same way.
                constraints = actor.grants[permission.key]
                if any(constraint.unrestricted for constraint in constraints):
                    return True
                placement = await self._relationships.placement(session, subject, on)
                return placement is not None and any(
                    _fits(constraint, placement) for constraint in constraints
                )

    # --- list filter -----------------------------------------------------------------

    def scope_filter(
        self,
        actor: Actor,
        base: str,
        employee_id_column: ColumnElement[uuid.UUID] | QueryableAttribute[uuid.UUID],
        *,
        on: date | None = None,
    ) -> ColumnElement[bool]:
        """A predicate on `employee_id_column` covering every scope the actor holds."""
        on = on or self.today()
        keys = [key for key in scoped_keys(base) if actor.holds(key)]
        if not keys:
            raise AccessDenied
        clauses: list[ColumnElement[bool]] = []
        for key in keys:
            match PERMISSIONS[key].scope:
                case Scope.ALL:
                    constraints = actor.grants[key]
                    if any(constraint.unrestricted for constraint in constraints):
                        return true()
                    clauses.extend(
                        employee_id_column.in_(
                            self._relationships.placed_employee_ids_query(c.department_id, c.location_id, on)
                        )
                        for c in constraints
                    )
                case Scope.TEAM if actor.employee_id is not None:
                    clauses.append(
                        employee_id_column.in_(
                            self._relationships.team_member_ids_query(actor.employee_id, on)
                        )
                    )
                case Scope.SELF if actor.employee_id is not None:
                    clauses.append(employee_id_column == actor.employee_id)
                case _:
                    pass
        return or_(*clauses) if clauses else false()


def _fits(constraint: Constraint, placement: Placement) -> bool:
    return (constraint.department_id is None or constraint.department_id == placement.department_id) and (
        constraint.location_id is None or constraint.location_id == placement.location_id
    )
