"""The access module's interface: the actor of each request (docs/authorization-model.md §1, §4).

`SessionActorProvider` implements `app.platform.authz.route.ActorProvider`. For every request
it authenticates the access cookie, then resolves effective permissions from PostgreSQL in the
same short transaction:

- assigned roles that apply now, each permission carrying the assignment's department and
  location constraint;
- derived roles: `employee` while the linked employee is employed today, `manager` while they
  have a direct report today;
- the account baseline every active account holds (own sessions and login history).

Nothing is cached, so revocations, expiries and reporting-line changes apply on the next
request. The transaction closes before the route runs, so a route never holds two pooled
connections at once.

`bootstrap_super_admins` is the installation step that creates the first two super admins
(docs/authorization-model.md §4.3, bootstrap).
"""

import uuid
from collections import defaultdict
from datetime import datetime

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access import repository as repo
from app.modules.access.models import UserRole
from app.modules.identity import public as identity
from app.modules.people import public as people
from app.platform.authz.context import UNRESTRICTED, Actor, Constraint, SessionScope
from app.platform.authz.roles import ACCOUNT_BASELINE, MIN_SUPER_ADMINS, RoleKey
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType


async def effective_grants(
    session: AsyncSession, user_id: uuid.UUID, employee_id: uuid.UUID | None, now: datetime
) -> tuple[dict[str, tuple[Constraint, ...]], frozenset[str]]:
    """Permission key -> constraints, and the role keys held, for an account now."""
    today = now.date()
    derived: list[str] = []
    if employee_id is not None and await people.is_employed(session, employee_id, today):
        derived.append(RoleKey.EMPLOYEE.value)
        if await people.has_direct_reports(session, employee_id, today):
            derived.append(RoleKey.MANAGER.value)
    rows = await repo.assigned_grants(session, user_id, now) + await repo.derived_grants(session, derived)

    constraints: defaultdict[str, set[Constraint]] = defaultdict(set)
    for key in ACCOUNT_BASELINE:
        constraints[key].add(UNRESTRICTED)
    for row in rows:
        constraints[row.permission_key].add(Constraint(row.department_id, row.location_id))
    grants = {
        key: (UNRESTRICTED,) if UNRESTRICTED in held else tuple(sorted(held, key=str))
        for key, held in constraints.items()
    }
    return grants, frozenset({row.role_key for row in rows} | set(derived))


class SessionActorProvider:
    def __init__(self, database: Database, clock: Clock) -> None:
        self._db = database
        self._clock = clock

    async def actor_for(self, request: Request) -> Actor:
        token = request.cookies.get(identity.ACCESS_COOKIE)
        if not token:
            raise ProblemError(ProblemType.UNAUTHENTICATED)
        now = self._clock.now()
        user_activity = request.headers.get(identity.BACKGROUND_HEADER) != "1"
        async with self._db.unit_of_work() as session:
            auth = await identity.authenticate(session, token, now, user_activity=user_activity)
            if auth is None:
                raise ProblemError(ProblemType.UNAUTHENTICATED)
            if auth.scope is SessionScope.FULL:
                grants, role_keys = await effective_grants(session, auth.user_id, auth.employee_id, now)
            else:
                # Enrolment-only sessions hold no permissions at all.
                grants, role_keys = {}, frozenset()
        return Actor(
            user_id=auth.user_id,
            session_id=auth.session_id,
            session_scope=auth.scope,
            employee_id=auth.employee_id,
            grants=grants,
            role_keys=role_keys,
            step_up_at=auth.step_up_at,
        )


class BootstrapRefusedError(Exception):
    """The installation already has super admins, or the request is not two new accounts."""


BOOTSTRAP_LOCK = 0x5356_4253  # "SVBS": serializes concurrent bootstrap attempts


async def bootstrap_super_admins(session: AsyncSession, emails: list[str], now: datetime) -> dict[str, str]:
    """Create two invited accounts holding `super_admin`; returns email -> invite token.

    Allowed once per installation: refused if any super admin assignment exists. SOD-8 needs
    a second super admin for every later assignment, so the first two are created together.
    The assignments have no `granted_by` (the database allows that only for the bootstrap).
    """
    if len(emails) != MIN_SUPER_ADMINS or len({email.casefold() for email in emails}) != MIN_SUPER_ADMINS:
        raise BootstrapRefusedError(f"give exactly {MIN_SUPER_ADMINS} different email addresses")
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK})
    role = await repo.role_by_key(session, RoleKey.SUPER_ADMIN.value)
    if role is None:
        raise RuntimeError("the super_admin role is missing; run the migrations first")
    if await repo.role_ever_assigned(session, role.id):
        raise BootstrapRefusedError("this installation already has super admins")
    invites: dict[str, str] = {}
    for email in emails:
        if await identity.email_in_use(session, email):
            raise BootstrapRefusedError(f"an account for {email} already exists")
        user_id = await identity.create_invited_account(session, email)
        session.add(
            UserRole(user_id=user_id, role_id=role.id, valid_from=now, grant_reason="Installation bootstrap")
        )
        invites[email] = await identity.issue_invite(session, user_id, now)
    return invites
