"""An administrator account linked to its holder's own employee record (decided by the project
owner on 2026-10-23, docs/authorization-model.md §4.2): employee self-service on the same
account, never the derived manager role or a manager's notifications, and no role added by
the link. All data is obviously fake (docs/database-design.md §10)."""

import uuid
from datetime import timedelta

import httpx

from app.modules.access import public as access
from app.modules.access.models import UserRole
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.integration.test_account_links import link
from tests.people_data import add_job, create_employee, create_org_units


async def linked_admin_with_report(api: Api, role: RoleKey) -> tuple[Account, uuid.UUID, uuid.UUID]:
    """An administrator account, its own employee record (work email = account email) with one
    direct report, and that report's ID. The account is not linked yet."""
    admin = await api.create_account(roles=(role.value,))
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        own = await create_employee(session, joined=today - timedelta(days=60))
        await add_job(session, units, own, manager_id=None, effective_from=today - timedelta(days=60))
        report = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(session, units, report, manager_id=own, effective_from=today - timedelta(days=30))
    assert await api.update("employees", {"_id": own}, {"work_email": admin.email}) == 1
    return admin, own, report


async def hr_links(api: Api, account: Account, employee: uuid.UUID) -> httpx.Response:
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, hr)
        await api.step_up(client, hr)
        return await client.post(f"/api/v1/users/{account.user_id}/employee-link", json=link(employee))


async def test_a_super_admin_linked_to_their_record_gets_self_service_only(api: Api) -> None:
    admin, own, report = await linked_admin_with_report(api, RoleKey.SUPER_ADMIN)
    roles_before = await api.count("user_roles", {"user_id": admin.user_id})
    linked = await hr_links(api, admin, own)
    assert linked.status_code == 200, linked.text
    # Linking assigns nothing.
    assert await api.count("user_roles", {"user_id": admin.user_id}) == roles_before

    async with api.client() as client:
        await api.sign_in(client, admin)
        me = (await client.get("/api/v1/me")).json()
        assert me["employee_id"] == str(own)
        assert set(me["roles"]) == {"super_admin", "employee"}
        # Self scope only: nothing about a team, and no employee data beyond their own.
        assert not [key for key in me["permissions"] if key.endswith(".team")]
        assert not [key for key in me["permissions"] if key.startswith("employee.") and key.endswith(".all")]
        # Own records, on the same account.
        assert (await client.get("/api/v1/me/profile")).status_code == 200
        assert (await client.get("/api/v1/me/attendance/today")).status_code == 200
        assert (await client.get("/api/v1/me/leave/balances")).status_code == 200
        # The direct report's records stay out of reach: no derived manager role.
        assert (await client.get(f"/api/v1/employees/{report}")).status_code in (403, 404)
        today = api.clock.now().date().isoformat()
        days = await client.get(
            f"/api/v1/employees/{report}/attendance/days", params={"from": today, "to": today}
        )
        assert days.status_code in (403, 404)
        # Still a super admin: role administration works as before.
        assert (await client.get("/api/v1/roles")).status_code == 200


async def test_a_linked_system_admin_gets_no_manager_role_but_an_employee_does(api: Api) -> None:
    admin, own, _ = await linked_admin_with_report(api, RoleKey.SYSTEM_ADMIN)
    assert (await hr_links(api, admin, own)).status_code == 200
    plain, plain_own, _ = await linked_admin_with_report(api, RoleKey.AUDITOR)
    assert (await hr_links(api, plain, plain_own)).status_code == 200
    async with api.client() as client:
        await api.sign_in(client, admin)
        assert set((await client.get("/api/v1/me")).json()["roles"]) == {"system_admin", "employee"}
    # The same reporting line gives an account without an administrator role the manager role.
    async with api.client() as client:
        await api.sign_in(client, plain)
        assert "manager" in (await client.get("/api/v1/me")).json()["roles"]


async def test_manager_notifications_skip_an_administrator_account(api: Api) -> None:
    admin, own, _ = await linked_admin_with_report(api, RoleKey.SUPER_ADMIN)
    assert (await hr_links(api, admin, own)).status_code == 200
    plain = await api.create_account()
    now = api.clock.now()
    async with api.database.unit_of_work() as session:
        assert await access.manager_account(session, admin.user_id, now) is None
        assert await access.manager_account(session, plain.user_id, now) == plain.user_id
        assert await access.manager_account(session, None, now) is None


async def test_the_manager_role_returns_once_the_administrator_role_is_removed(api: Api) -> None:
    admin, own, report = await linked_admin_with_report(api, RoleKey.SYSTEM_ADMIN)
    assert (await hr_links(api, admin, own)).status_code == 200
    [held] = await api.records(UserRole, {"user_id": admin.user_id})
    remover = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, remover)
        await api.step_up(client, remover)
        removed = await client.delete(f"/api/v1/users/{admin.user_id}/roles/{held.id}")
        assert removed.status_code == 204, removed.text
    async with api.client() as client:
        await api.sign_in(client, admin)
        assert set((await client.get("/api/v1/me")).json()["roles"]) == {"employee", "manager"}
        assert (await client.get(f"/api/v1/employees/{report}")).status_code == 200
