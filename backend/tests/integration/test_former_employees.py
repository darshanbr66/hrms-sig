"""Former employees and the account list (docs/api-architecture.md §8 Administration).

The account list (`GET /users`) leaves out the accounts of people who have been offboarded;
they are listed with their last job and account state on `GET /former-employees`, which reads
through `employee.profile.read` at the reader's scope. Ordinary disabled accounts, invites and
accounts without an employee record stay on the account list. Erased records are on neither
list. Nothing is changed by either. All data is obviously fake (docs/database-design.md §10).
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from app.modules.identity.models import UserStatus
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api, running_api
from tests.conftest import MongoServer
from tests.people_data import OrgUnits, add_job, create_employee, create_org_units

FORMER = "/api/v1/former-employees"


async def signed_in(api: Api, client: httpx.AsyncClient, account: Account) -> None:
    await api.sign_in(client, account)
    await api.step_up(client, account)


async def employee(api: Api, units: OrgUnits, *, roles: tuple[str, ...] = ()) -> Account:
    """An account linked to an employee record employed for the last 90 days."""
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        employee_id = await create_employee(session, joined=today - timedelta(days=90))
        await add_job(session, units, employee_id, manager_id=None, effective_from=today - timedelta(days=90))
    return await api.create_account(employee_id=employee_id, roles=roles)


async def no_account(api: Api, units: OrgUnits) -> uuid.UUID:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        employee_id = await create_employee(session, joined=today - timedelta(days=90))
        await add_job(session, units, employee_id, manager_id=None, effective_from=today - timedelta(days=90))
    return employee_id


async def offboard(api: Api, client: httpx.AsyncClient, employee_id: uuid.UUID, days_ago: int) -> str:
    left = await client.post(
        f"/api/v1/employees/{employee_id}/exit",
        json={
            "last_working_day": (api.clock.now().date() - timedelta(days=days_ago)).isoformat(),
            "reason": "Test: resigned",
        },
    )
    assert left.status_code == 201, left.text
    return str(left.json()["account"])


@dataclass
class People:
    hr: Account
    current: Account
    disabled: Account
    invited: Account
    unlinked: Account
    leaver: Account
    admin_leaver: Account
    leaver_without_account: uuid.UUID
    erased: Account
    other_units: OrgUnits


async def people(api: Api) -> People:
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        other_units = await create_org_units(session)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    current = await employee(api, units)
    disabled = await employee(api, units)
    invited = await api.create_account(status=UserStatus.INVITED)
    unlinked = await api.create_account()
    leaver = await employee(api, units)
    admin_leaver = await employee(api, other_units, roles=(RoleKey.SYSTEM_ADMIN.value,))
    leaver_without_account = await no_account(api, units)
    erased = await employee(api, units)
    async with api.client() as client:
        await signed_in(api, client, hr)
        assert (await client.post(f"/api/v1/users/{disabled.user_id}/disable")).status_code == 200
        assert await offboard(api, client, leaver.employee_id, 10) == "disabled"  # type: ignore[arg-type]
        # The exit leaves an administrator account active for a system admin to disable.
        assert await offboard(api, client, admin_leaver.employee_id, 5) == "administrator_left_active"  # type: ignore[arg-type]
        assert await offboard(api, client, leaver_without_account, 20) == "none"
        await offboard(api, client, erased.employee_id, 30)  # type: ignore[arg-type]
    # The state the erasure leaves (test_employee_erasure.py runs the whole erasure).
    now = api.clock.now()
    await api.update(
        "employees", {"_id": erased.employee_id}, {"erased_at": now, "legal_first_name": "Erased"}
    )
    await api.update(
        "users",
        {"_id": erased.user_id},
        {"erased_at": now, "email": f"erased-{erased.user_id.hex}@erased.invalid"},
    )
    return People(
        hr,
        current,
        disabled,
        invited,
        unlinked,
        leaver,
        admin_leaver,
        leaver_without_account,
        erased,
        other_units,
    )


async def all_pages(client: httpx.AsyncClient, path: str, **params: str | int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        response = await client.get(path, params={**params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            return items


async def test_the_account_list_leaves_offboarded_people_to_the_former_employees_list(
    isolated_api: Api,
) -> None:
    api = isolated_api
    p = await people(api)
    async with api.client() as client:
        await signed_in(api, client, p.hr)
        # Paged two at a time: the filter is applied before paging, so no page comes up short
        # and no account is skipped.
        listed = {item["id"] for item in await all_pages(client, "/api/v1/users", limit=2)}
        for kept in (p.hr, p.current, p.disabled, p.invited, p.unlinked):
            assert str(kept.user_id) in listed
        for moved in (p.leaver, p.admin_leaver, p.erased):
            assert str(moved.user_id) not in listed
        # Still found when asked for, and when the account of one record is looked up.
        everyone = {
            item["id"] for item in await all_pages(client, "/api/v1/users", include_offboarded="true")
        }
        assert {str(p.leaver.user_id), str(p.admin_leaver.user_id)} <= everyone
        one = await client.get("/api/v1/users", params={"employee_id": str(p.leaver.employee_id)})
        assert [item["id"] for item in one.json()["items"]] == [str(p.leaver.user_id)]
        searched = await client.get("/api/v1/users", params={"q": p.leaver.email})
        assert searched.json()["items"] == []
        # The ordinary disabled account keeps its Enable account path.
        disabled = await client.get(f"/api/v1/users/{p.disabled.user_id}")
        assert (disabled.json()["status"], disabled.json()["enable_blocked"]) == ("disabled", None)


async def test_former_employees_are_listed_with_their_account_state(isolated_api: Api) -> None:
    api = isolated_api
    p = await people(api)
    async with api.client() as client:
        await signed_in(api, client, p.hr)
        page = (await client.get(FORMER)).json()
        assert page["accounts_visible"] is True
        rows = {item["id"]: item for item in page["items"]}
        # Newest exit first; never the current staff, never an erased record.
        assert list(rows) == [
            str(p.admin_leaver.employee_id),
            str(p.leaver.employee_id),
            str(p.leaver_without_account),
        ]
        assert rows[str(p.leaver.employee_id)]["account"]["status"] == "disabled"
        assert rows[str(p.admin_leaver.employee_id)]["account"]["status"] == "active"
        assert rows[str(p.leaver_without_account)]["account"] is None
        assert rows[str(p.leaver.employee_id)]["job"]["department"]["id"]
        # Filters and search, on the server.
        active = (await client.get(FORMER, params={"account": "active"})).json()["items"]
        assert [item["id"] for item in active] == [str(p.admin_leaver.employee_id)]
        none = (await client.get(FORMER, params={"account": "none"})).json()["items"]
        assert [item["id"] for item in none] == [str(p.leaver_without_account)]
        by_unit = (
            await client.get(FORMER, params={"department_id": str(p.other_units.department_id)})
        ).json()
        assert [item["id"] for item in by_unit["items"]] == [str(p.admin_leaver.employee_id)]
        code = rows[str(p.leaver.employee_id)]["employee_code"]
        found = (await client.get(FORMER, params={"q": code.lower()})).json()["items"]
        assert [item["id"] for item in found] == [str(p.leaver.employee_id)]
        assert (await client.get(FORMER, params={"q": "Erased"})).json()["items"] == []
        paged = [item["id"] for item in await all_pages(client, FORMER, limit=1, sort="name")]
        assert sorted(paged) == sorted(rows)


async def test_who_sees_former_employees_and_their_accounts(isolated_api: Api) -> None:
    api = isolated_api
    p = await people(api)
    payroll = await api.create_account(roles=(RoleKey.PAYROLL_ADMIN.value,))
    async with api.client() as client:
        await signed_in(api, client, payroll)
        page = (await client.get(FORMER)).json()
        # People, but not accounts: payroll admins do not hold `user.read.all`.
        assert page["accounts_visible"] is False
        assert len(page["items"]) == 3
        assert all(item["account"] is None for item in page["items"])
        assert (await client.get(FORMER, params={"account": "active"})).status_code == 403
    for roles in ((RoleKey.SYSTEM_ADMIN.value,), (RoleKey.SUPER_ADMIN.value,), (RoleKey.AUDITOR.value,)):
        actor = await api.create_account(roles=roles)
        async with api.client() as client:
            await signed_in(api, client, actor)
            assert (await client.get(FORMER)).status_code == 403, roles
    # An employee reads only their own record: no former colleagues.
    async with api.client() as client:
        await signed_in(api, client, p.current)
        assert (await client.get(FORMER)).json()["items"] == []


async def test_reading_the_lists_changes_nothing_and_offboarding_rules_hold(isolated_api: Api) -> None:
    api = isolated_api
    p = await people(api)
    before = {
        name: await api.count(name, {})
        for name in ("users", "employees", "employee_jobs", "user_roles", "employee_status_history")
    }
    async with api.client() as client:
        await signed_in(api, client, p.hr)
        await client.get(FORMER)
        await client.get("/api/v1/users")
        refused = await client.post(f"/api/v1/users/{p.leaver.user_id}/enable")
        assert (refused.status_code, refused.json()["code"]) == (409, "user.employee_exited")
    after = {name: await api.count(name, {}) for name in before}
    assert after == before


async def test_another_organizations_former_employees_stay_out_of_reach(migrated_mongo: MongoServer) -> None:
    async with running_api(migrated_mongo) as a, running_api(migrated_mongo) as b:
        p = await people(a)
        b_hr = await b.create_account(roles=(RoleKey.HR.value,))
        b.clock.current = max(a.clock.current, b.clock.current)
        async with b.client() as client:
            await signed_in(b, client, b_hr)
            assert (await client.get(FORMER)).json()["items"] == []
            assert (await client.get(FORMER, params={"q": p.leaver.email})).json()["items"] == []
            everyone = await client.get("/api/v1/users", params={"include_offboarded": "true", "limit": 100})
            assert str(p.leaver.user_id) not in {item["id"] for item in everyone.json()["items"]}
