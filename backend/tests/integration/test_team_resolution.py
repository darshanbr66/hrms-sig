"""Team resolution over effective-dated reporting lines (docs/authorization-model.md §2, §7).

Covers the threat-model T4 transfer scenario (before, on and after a transfer), exits,
future joiners, a reporting-line cycle, and embedding the team query in a scope filter.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.modules.people import public as people
from app.modules.people.models import Employee, JobChangeReason
from app.platform.db import Database
from tests.people_data import add_job, create_employee, create_org_units

JOINED = date(2030, 1, 1)
ON = date(2030, 6, 1)


async def test_team_is_the_whole_reporting_subtree(app_database: Database) -> None:
    async with app_database.unit_of_work() as session:
        units = await create_org_units(session)
        head, a, b, c, d = [await create_employee(session, joined=JOINED) for _ in range(5)]
        await add_job(session, units, head, manager_id=None, effective_from=JOINED)
        await add_job(session, units, a, manager_id=head, effective_from=JOINED)
        await add_job(session, units, b, manager_id=a, effective_from=JOINED)
        await add_job(session, units, c, manager_id=b, effective_from=JOINED)
        await add_job(session, units, d, manager_id=a, effective_from=JOINED)

    async with app_database.unit_of_work() as session:
        assert await people.team_member_ids(session, head, ON) == {a, b, c, d}
        assert await people.team_member_ids(session, a, ON) == {b, c, d}
        assert await people.team_member_ids(session, b, ON) == {c}
        assert await people.team_member_ids(session, c, ON) == frozenset()

        assert await people.is_team_member(session, a, c, ON)
        assert await people.is_team_member(session, head, c, ON)
        assert not await people.is_team_member(session, b, d, ON)
        assert not await people.is_team_member(session, c, a, ON)
        assert not await people.is_team_member(session, a, a, ON)

        assert await people.has_direct_reports(session, a, ON)
        assert not await people.has_direct_reports(session, c, ON)


async def test_nobody_reports_before_the_reporting_line_starts(app_database: Database) -> None:
    async with app_database.unit_of_work() as session:
        units = await create_org_units(session)
        manager, report = [await create_employee(session, joined=JOINED) for _ in range(2)]
        await add_job(session, units, manager, manager_id=None, effective_from=JOINED)
        await add_job(session, units, report, manager_id=manager, effective_from=JOINED)

    async with app_database.unit_of_work() as session:
        before = JOINED - timedelta(days=1)
        assert await people.team_member_ids(session, manager, before) == frozenset()
        assert not await people.is_team_member(session, manager, report, before)
        assert not await people.has_direct_reports(session, manager, before)


async def test_future_dated_transfer_moves_the_employee_on_its_effective_date(app_database: Database) -> None:
    """Before the transfer date the open-ended job row is the future one; access must not
    move early. On the transfer date it moves; the old manager keeps no current access."""
    transfer = date(2030, 9, 1)
    async with app_database.unit_of_work() as session:
        units = await create_org_units(session)
        old_manager, new_manager, employee = [await create_employee(session, joined=JOINED) for _ in range(3)]
        await add_job(session, units, old_manager, manager_id=None, effective_from=JOINED)
        await add_job(session, units, new_manager, manager_id=None, effective_from=JOINED)
        await add_job(
            session, units, employee, manager_id=old_manager, effective_from=JOINED, effective_to=transfer
        )
        await add_job(
            session,
            units,
            employee,
            manager_id=new_manager,
            effective_from=transfer,
            reason=JobChangeReason.TRANSFER,
        )

    async with app_database.unit_of_work() as session:
        day_before = transfer - timedelta(days=1)
        assert await people.team_member_ids(session, old_manager, day_before) == {employee}
        assert await people.team_member_ids(session, new_manager, day_before) == frozenset()
        assert await people.is_team_member(session, old_manager, employee, day_before)
        assert not await people.is_team_member(session, new_manager, employee, day_before)

        for on in (transfer, transfer + timedelta(days=30)):
            assert await people.team_member_ids(session, old_manager, on) == frozenset()
            assert await people.team_member_ids(session, new_manager, on) == {employee}
            assert not await people.is_team_member(session, old_manager, employee, on)
            assert await people.is_team_member(session, new_manager, employee, on)

        # A record from before the transfer still belongs to the old manager's team as of its date.
        assert await people.is_team_member(session, old_manager, employee, date(2030, 3, 15))


async def test_exited_and_not_yet_joined_employees_are_not_team_members(app_database: Database) -> None:
    exit_date = date(2030, 4, 30)
    joins_later = date(2030, 8, 1)
    async with app_database.unit_of_work() as session:
        units = await create_org_units(session)
        manager = await create_employee(session, joined=JOINED)
        leaver = await create_employee(session, joined=JOINED, exited=exit_date)
        joiner = await create_employee(session, joined=joins_later)
        await add_job(session, units, manager, manager_id=None, effective_from=JOINED)
        # The leaver's job row is still open: team resolution must not rely on it being closed.
        await add_job(session, units, leaver, manager_id=manager, effective_from=JOINED)
        await add_job(session, units, joiner, manager_id=manager, effective_from=joins_later)

    async with app_database.unit_of_work() as session:
        assert await people.team_member_ids(session, manager, exit_date) == {leaver}
        assert await people.team_member_ids(session, manager, ON) == frozenset()
        assert not await people.has_direct_reports(session, manager, ON)
        assert await people.team_member_ids(session, manager, joins_later) == {joiner}


async def test_reports_of_a_departed_manager_leave_the_upper_team(app_database: Database) -> None:
    """Access follows current employment: a report under an exited middle manager is not
    reached through them until HR assigns a new manager."""
    async with app_database.unit_of_work() as session:
        units = await create_org_units(session)
        head = await create_employee(session, joined=JOINED)
        middle = await create_employee(session, joined=JOINED, exited=date(2030, 3, 31))
        report = await create_employee(session, joined=JOINED)
        await add_job(session, units, head, manager_id=None, effective_from=JOINED)
        await add_job(session, units, middle, manager_id=head, effective_from=JOINED)
        await add_job(session, units, report, manager_id=middle, effective_from=JOINED)

    async with app_database.unit_of_work() as session:
        assert await people.team_member_ids(session, head, date(2030, 2, 1)) == {middle, report}
        assert await people.team_member_ids(session, head, ON) == frozenset()
        assert not await people.is_team_member(session, head, report, ON)


async def test_a_reporting_line_cycle_terminates(app_database: Database) -> None:
    """The M2 write path prevents cycles; resolution must still terminate if one exists."""
    async with app_database.unit_of_work() as session:
        units = await create_org_units(session)
        first, second, third = [await create_employee(session, joined=JOINED) for _ in range(3)]
        await add_job(session, units, first, manager_id=third, effective_from=JOINED)
        await add_job(session, units, second, manager_id=first, effective_from=JOINED)
        await add_job(session, units, third, manager_id=second, effective_from=JOINED)

    async with app_database.unit_of_work() as session:
        assert await people.team_member_ids(session, first, ON) == {second, third}
        assert await people.is_team_member(session, first, third, ON)
        assert await people.is_team_member(session, third, first, ON)
        assert not await people.is_team_member(session, first, first, ON)


async def test_team_query_embeds_in_a_scope_filter(app_database: Database) -> None:
    async with app_database.unit_of_work() as session:
        units = await create_org_units(session)
        manager, report, outsider = [await create_employee(session, joined=JOINED) for _ in range(3)]
        await add_job(session, units, manager, manager_id=None, effective_from=JOINED)
        await add_job(session, units, report, manager_id=manager, effective_from=JOINED)
        await add_job(session, units, outsider, manager_id=None, effective_from=JOINED)

    async with app_database.unit_of_work() as session:
        in_scope = select(Employee.id).where(
            Employee.id.in_(people.team_member_ids_query(manager, ON)),
            Employee.id.in_([report, outsider]),
        )
        assert set(await session.scalars(in_scope)) == {report}


async def test_unknown_employees_have_no_team(app_database: Database) -> None:
    unknown = uuid.uuid4()
    async with app_database.unit_of_work() as session:
        assert await people.team_member_ids(session, unknown, ON) == frozenset()
        assert not await people.has_direct_reports(session, unknown, ON)
        assert not await people.is_team_member(session, unknown, uuid.uuid4(), ON)
