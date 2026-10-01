"""Scopes over real data (docs/authorization-model.md §2, §5, §7).

Actors are built the way every request builds them (`effective_grants` over role assignments,
employment and reporting lines in PostgreSQL), then the engine decides single records and
produces list filters that are executed against the database.
"""

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access.public import effective_grants
from app.modules.people import public as people
from app.modules.people.models import Employee
from app.platform.authz.context import Actor, SessionScope
from app.platform.authz.engine import AccessDenied, Authorizer, StepUpRequired
from app.platform.authz.roles import RoleKey
from app.platform.clock import Clock
from tests.api_support import Api
from tests.people_data import OrgUnits, add_job, create_employee, create_org_units

TODAY = date(2030, 6, 1)
NOW = datetime(2030, 6, 1, 9, tzinfo=UTC)
JOINED = date(2030, 1, 1)


class At(Clock):
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


async def actor_for(
    session: AsyncSession, user_id: uuid.UUID, employee_id: uuid.UUID | None, now: datetime = NOW
) -> Actor:
    grants, role_keys = await effective_grants(session, user_id, employee_id, now)
    return Actor(
        user_id=user_id,
        session_id=uuid.uuid4(),
        session_scope=SessionScope.FULL,
        employee_id=employee_id,
        grants=grants,
        role_keys=role_keys,
    )


async def visible(
    session: AsyncSession, engine: Authorizer, actor: Actor, base: str, among: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Run a list query with the engine's scope filter, restricted to the test's employees."""
    query = select(Employee.id).where(Employee.id.in_(among), engine.scope_filter(actor, base, Employee.id))
    return set(await session.scalars(query))


async def allowed(
    session: AsyncSession,
    engine: Authorizer,
    actor: Actor,
    base: str,
    subject: uuid.UUID,
    *,
    on: date = TODAY,
) -> bool:
    try:
        await engine.require(session, actor, base, subject_employee_id=subject, on=on)
    except AccessDenied:
        return False
    return True


async def team_with_two_departments(session: AsyncSession) -> tuple[OrgUnits, OrgUnits, dict[str, uuid.UUID]]:
    a, b = await create_org_units(session), await create_org_units(session)
    people_ids = {
        name: await create_employee(session, joined=JOINED) for name in ("head", "lead", "dev", "other")
    }
    await add_job(session, a, people_ids["head"], manager_id=None, effective_from=JOINED)
    await add_job(session, a, people_ids["lead"], manager_id=people_ids["head"], effective_from=JOINED)
    await add_job(session, a, people_ids["dev"], manager_id=people_ids["lead"], effective_from=JOINED)
    await add_job(session, b, people_ids["other"], manager_id=None, effective_from=JOINED)
    return a, b, people_ids


@pytest.fixture
def engine() -> Authorizer:
    return Authorizer(people.relationships, At(NOW))


async def test_employee_and_manager_roles_come_from_employment_and_reporting_lines(
    api: Api, engine: Authorizer
) -> None:
    async with api.database.unit_of_work() as session:
        _, _, ids = await team_with_two_departments(session)
    lead = await api.create_account(employee_id=ids["lead"])
    dev = await api.create_account(employee_id=ids["dev"])
    unlinked = await api.create_account()
    async with api.database.unit_of_work() as session:
        lead_actor = await actor_for(session, lead.user_id, ids["lead"])
        dev_actor = await actor_for(session, dev.user_id, ids["dev"])
        nobody = await actor_for(session, unlinked.user_id, None)
        among = set(ids.values())

        assert {"employee", "manager"} <= lead_actor.role_keys
        assert "manager" not in dev_actor.role_keys
        assert nobody.role_keys == frozenset()
        assert nobody.permission_keys == {"auth.session.read.self", "auth.session.revoke.self"}

        assert await visible(session, engine, lead_actor, "employee.profile.read", among) == {
            ids["lead"],
            ids["dev"],
        }
        assert await visible(session, engine, dev_actor, "employee.profile.read", among) == {ids["dev"]}
        with pytest.raises(AccessDenied):
            await visible(session, engine, nobody, "employee.profile.read", among)

        assert await allowed(session, engine, lead_actor, "employee.profile.read", ids["dev"])
        assert not await allowed(session, engine, dev_actor, "employee.profile.read", ids["lead"])
        assert not await allowed(session, engine, lead_actor, "employee.profile.read", ids["head"])
        # Managers have no personal or sensitive access to their team.
        assert not await allowed(session, engine, lead_actor, "employee.personal.read", ids["dev"])
        assert await allowed(session, engine, dev_actor, "employee.personal.read", ids["dev"])


async def test_hr_restricted_to_a_department_or_location(api: Api, engine: Authorizer) -> None:
    async with api.database.unit_of_work() as session:
        a, b, ids = await team_with_two_departments(session)
    among = set(ids.values())
    for scope, expected in (
        ({"department_id": a.department_id}, {ids["head"], ids["lead"], ids["dev"]}),
        ({"department_id": b.department_id}, {ids["other"]}),
        ({"location_id": b.location_id}, {ids["other"]}),
        ({"department_id": a.department_id, "location_id": b.location_id}, set()),
        ({}, among),
    ):
        hr = await api.create_account(roles=(RoleKey.HR.value,), role_scope=scope)
        async with api.database.unit_of_work() as session:
            actor = await actor_for(session, hr.user_id, None)
            assert await visible(session, engine, actor, "employee.profile.read", among) == expected, scope
            for employee_id in among:
                assert await allowed(session, engine, actor, "employee.profile.read", employee_id) == (
                    employee_id in expected
                ), scope


async def test_two_assignments_combine(api: Api, engine: Authorizer) -> None:
    async with api.database.unit_of_work() as session:
        a, b, ids = await team_with_two_departments(session)
    hr = await api.create_account(roles=(RoleKey.HR.value,), role_scope={"department_id": a.department_id})
    await api.execute(
        "INSERT INTO access.user_roles (user_id, role_id, department_id, valid_from, grant_reason) "
        "SELECT :user, id, :department, now() - interval '1 minute', 'Cover' "
        "FROM access.roles WHERE key = 'hr_admin'",
        user=hr.user_id,
        department=b.department_id,
    )
    async with api.database.unit_of_work() as session:
        actor = await actor_for(session, hr.user_id, None)
        assert await visible(session, engine, actor, "employee.profile.read", set(ids.values())) == set(
            ids.values()
        )
        # hr_admin's extra permissions apply only in department B, and only after step-up.
        with pytest.raises(StepUpRequired):
            await allowed(session, engine, actor, "employee.sensitive.read", ids["other"])
        stepped_up = replace(actor, step_up_at=NOW)
        assert await allowed(session, engine, stepped_up, "employee.sensitive.read", ids["other"])
        assert not await allowed(session, engine, stepped_up, "employee.sensitive.read", ids["dev"])


async def test_a_future_dated_manager_change_moves_access_on_its_date(api: Api) -> None:
    transfer = date(2030, 9, 1)
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        old, new, report = [await create_employee(session, joined=JOINED) for _ in range(3)]
        for manager in (old, new):
            await add_job(session, units, manager, manager_id=None, effective_from=JOINED)
        await add_job(session, units, report, manager_id=old, effective_from=JOINED, effective_to=transfer)
        await add_job(session, units, report, manager_id=new, effective_from=transfer)
    old_account = await api.create_account(employee_id=old)
    new_account = await api.create_account(employee_id=new)
    for moment, old_sees, new_sees in (
        (datetime(2030, 8, 31, 23, 59, tzinfo=UTC), True, False),
        (datetime(2030, 9, 1, 0, 0, tzinfo=UTC), False, True),
    ):
        engine = Authorizer(people.relationships, At(moment))
        async with api.database.unit_of_work() as session:
            old_actor = await actor_for(session, old_account.user_id, old, moment)
            new_actor = await actor_for(session, new_account.user_id, new, moment)
            assert ("manager" in old_actor.role_keys) is old_sees
            assert ("manager" in new_actor.role_keys) is new_sees
            on = moment.date()
            assert (
                await allowed(session, engine, new_actor, "employee.profile.read", report, on=on) is new_sees
            )
            assert (
                await allowed(session, engine, old_actor, "employee.profile.read", report, on=on) is old_sees
            )
        # The old manager keeps no current access, but a record dated before the transfer
        # belongs to their team as of its date.
    async with api.database.unit_of_work() as session:
        engine = Authorizer(people.relationships, At(datetime(2030, 10, 1, tzinfo=UTC)))
        # Only a manager can reach team scope; once they have no reports they hold none.
        old_actor = await actor_for(session, old_account.user_id, old, datetime(2030, 10, 1, tzinfo=UTC))
        assert "manager" not in old_actor.role_keys


async def test_losing_the_last_direct_report_removes_the_manager_role(api: Api) -> None:
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        manager, report = [await create_employee(session, joined=JOINED) for _ in range(2)]
        await add_job(session, units, manager, manager_id=None, effective_from=JOINED)
        await add_job(
            session, units, report, manager_id=manager, effective_from=JOINED, effective_to=date(2030, 7, 1)
        )
        await add_job(session, units, report, manager_id=None, effective_from=date(2030, 7, 1))
    account = await api.create_account(employee_id=manager)
    async with api.database.unit_of_work() as session:
        before = await actor_for(session, account.user_id, manager, datetime(2030, 6, 30, tzinfo=UTC))
        after = await actor_for(session, account.user_id, manager, datetime(2030, 7, 1, tzinfo=UTC))
    assert "employee.profile.read.team" in before.permission_keys
    assert "employee.profile.read.team" not in after.permission_keys


async def test_an_exited_employee_holds_no_employee_role(api: Api) -> None:
    async with api.database.unit_of_work() as session:
        leaver = await create_employee(session, joined=JOINED, exited=date(2030, 5, 31))
    account = await api.create_account(employee_id=leaver)
    async with api.database.unit_of_work() as session:
        on_last_day = await actor_for(session, account.user_id, leaver, datetime(2030, 5, 31, 12, tzinfo=UTC))
        after = await actor_for(session, account.user_id, leaver, NOW)
    assert "employee" in on_last_day.role_keys
    assert after.role_keys == frozenset()
    assert "employee.profile.read.self" not in after.permission_keys


async def test_admin_roles_reach_no_employee_data(api: Api, engine: Authorizer) -> None:
    async with api.database.unit_of_work() as session:
        _, _, ids = await team_with_two_departments(session)
    for role in (RoleKey.SYSTEM_ADMIN, RoleKey.SUPER_ADMIN, RoleKey.AUDITOR):
        account = await api.create_account(roles=(role.value,))
        async with api.database.unit_of_work() as session:
            actor = await actor_for(session, account.user_id, None)
            for base in ("employee.profile.read", "employee.personal.read", "attendance.read", "leave.read"):
                with pytest.raises(AccessDenied):
                    await visible(session, engine, actor, base, set(ids.values()))
                assert not await allowed(session, engine, actor, base, ids["dev"])


def test_dates_are_today_in_utc() -> None:
    assert Authorizer(people.relationships, At(NOW)).today() == TODAY
    assert Authorizer(people.relationships, At(NOW - timedelta(hours=10))).today() == TODAY - timedelta(
        days=1
    )
