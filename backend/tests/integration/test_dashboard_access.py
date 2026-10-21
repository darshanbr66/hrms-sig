"""What each role's dashboard can load (docs/ui-ux-guidelines.md §2 Home; authorization-model §4).

The dashboard shows a widget only to people holding its permission, and the API decides every
read. These tests ask the dashboard's own data sources as each role and assert who gets data
and who is refused, and that a super admin reaches the HR figures only through an approved,
time-bound elevation, which stops applying at its end.
"""

from datetime import timedelta

import pytest

from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.people_data import add_job, create_employee, create_org_units

# The dashboard's data sources (frontend/src/features/dashboard/DashboardPage.tsx).
STATISTICS = "/api/v1/employees/statistics"
ATTENDANCE_TODAY = "/api/v1/attendance/today"
LEAVE_WAITING = "/api/v1/leave-requests?awaiting_my_decision=true&limit=5"
ACCESS_REQUESTS = "/api/v1/role-grant-requests?status=pending&limit=5"
DEPARTMENTS = "/api/v1/departments"
MY_LEAVE = "/api/v1/me/leave/balances"
SIGN_INS = "/api/v1/me/login-history?limit=3"

# Expected status per role. 404 on MY_LEAVE: an account without an employee record has no
# leave of its own.
EXPECTED: dict[str, dict[str, int]] = {
    "super_admin": {
        STATISTICS: 403,
        ATTENDANCE_TODAY: 403,
        LEAVE_WAITING: 403,
        ACCESS_REQUESTS: 200,
        DEPARTMENTS: 403,
        MY_LEAVE: 403,
        SIGN_INS: 200,
    },
    "hr_admin": {
        STATISTICS: 200,
        ATTENDANCE_TODAY: 200,
        LEAVE_WAITING: 200,
        ACCESS_REQUESTS: 200,
        DEPARTMENTS: 200,
        MY_LEAVE: 404,
        SIGN_INS: 200,
    },
    "hr": {
        STATISTICS: 200,
        ATTENDANCE_TODAY: 200,
        LEAVE_WAITING: 200,
        ACCESS_REQUESTS: 403,
        DEPARTMENTS: 200,
        MY_LEAVE: 404,
        SIGN_INS: 200,
    },
    "manager": {
        STATISTICS: 200,
        ATTENDANCE_TODAY: 200,
        LEAVE_WAITING: 200,
        ACCESS_REQUESTS: 403,
        DEPARTMENTS: 200,
        MY_LEAVE: 200,
        SIGN_INS: 200,
    },
    # Scoped to their own record (`employee.profile.read.self`); the dashboard does not show it.
    "employee": {
        STATISTICS: 200,
        ATTENDANCE_TODAY: 200,
        LEAVE_WAITING: 403,
        ACCESS_REQUESTS: 403,
        DEPARTMENTS: 200,
        MY_LEAVE: 200,
        SIGN_INS: 200,
    },
}


async def account_for(api: Api, role: str) -> Account:
    if role not in ("manager", "employee"):
        return await api.create_account(roles=(role,))
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        person = await create_employee(session, joined=today - timedelta(days=100))
        await add_job(session, units, person, manager_id=None, effective_from=today - timedelta(days=100))
        if role == "manager":
            report = await create_employee(session, joined=today - timedelta(days=100))
            await add_job(
                session, units, report, manager_id=person, effective_from=today - timedelta(days=100)
            )
    return await api.create_account(employee_id=person)


@pytest.mark.parametrize("role", list(EXPECTED))
async def test_each_role_loads_exactly_its_dashboard_sources(api: Api, role: str) -> None:
    account = await account_for(api, role)
    async with api.client() as client:
        await api.sign_in(client, account)
        got = {path: (await client.get(path)).status_code for path in EXPECTED[role]}
    assert got == EXPECTED[role]


async def test_an_employee_s_figures_cover_only_themselves(api: Api) -> None:
    employee = await account_for(api, "employee")
    async with api.client() as client:
        await api.sign_in(client, employee)
        stats = (await client.get(STATISTICS)).json()
        assert stats["employed"] == 1
        assert stats["recent_joiners"] == []


async def test_a_manager_s_figures_cover_only_their_team(api: Api) -> None:
    manager = await account_for(api, "manager")
    async with api.client() as client:
        await api.sign_in(client, manager)
        stats = (await client.get(STATISTICS)).json()
        # The manager and their one report; nobody else in the shared test database.
        assert stats["employed"] == 2
        assert sum(item["employed"] for item in stats["by_department"]) == 2


async def test_the_department_link_lists_only_the_manager_s_team(api: Api) -> None:
    """The department hierarchy links to `/employees?department_id=…`; the filter narrows the
    reader's scope and never widens it, so a colleague in the same department who is not on
    the manager's team stays out of the list."""
    today = api.clock.now().date()
    joined = today - timedelta(days=100)
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        manager = await create_employee(session, joined=joined)
        await add_job(session, units, manager, manager_id=None, effective_from=joined)
        report = await create_employee(session, joined=joined)
        await add_job(session, units, report, manager_id=manager, effective_from=joined)
        colleague = await create_employee(session, joined=joined)
        await add_job(session, units, colleague, manager_id=None, effective_from=joined)
    account = await api.create_account(employee_id=manager)
    async with api.client() as client:
        await api.sign_in(client, account)
        response = await client.get("/api/v1/employees", params={"department_id": str(units.department_id)})
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {str(manager), str(report)}


async def test_a_super_admin_reaches_the_hr_figures_only_through_an_approved_elevation(
    isolated_api: Api,
) -> None:
    api = isolated_api
    requester = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    approver = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    async with api.client() as mine, api.client() as theirs:
        await api.sign_in(mine, requester)
        await api.sign_in(theirs, approver)
        assert (await mine.get(STATISTICS)).status_code == 403

        # Requesting needs step-up.
        body = {"role_key": "hr_admin", "reason": "Test: check the HR dashboard", "duration_minutes": 60}
        assert (await mine.post("/api/v1/role-grant-requests", json=body)).status_code == 403
        await api.step_up(mine, requester)
        request = (await mine.post("/api/v1/role-grant-requests", json=body)).json()
        assert request["status"] == "pending"
        # Nobody approves their own request (SOD-9).
        own = await mine.post(f"/api/v1/role-grant-requests/{request['id']}/approve", json={})
        assert own.status_code == 403

        await api.step_up(theirs, approver)
        approved = await theirs.post(f"/api/v1/role-grant-requests/{request['id']}/approve", json={})
        assert approved.json()["status"] == "active"

        assert (await mine.post("/api/v1/auth/refresh")).status_code == 200
        me = (await mine.get("/api/v1/me")).json()
        assert "hr_admin" in me["roles"]
        assert {"employee.profile.read.all", "org.manage", "leave.request.approve.all"} <= set(
            me["permissions"]
        )
        for path in (STATISTICS, ATTENDANCE_TODAY, LEAVE_WAITING, DEPARTMENTS):
            assert (await mine.get(path)).status_code == 200, path
        # The approver stays a plain super admin.
        assert (await theirs.get(STATISTICS)).status_code == 403

    # The elevation ends by itself after its window: signed in again afterwards, the account is a
    # plain super admin.
    api.clock.advance(timedelta(minutes=61))
    async with api.client() as later:
        await api.sign_in(later, requester)
        assert (await later.get(STATISTICS)).status_code == 403
        assert "hr_admin" not in (await later.get("/api/v1/me")).json()["roles"]

    actions = await api.records(
        AuditLogRecord, {"target_id": request["id"]}, sort=[("recorded_at", 1), ("id", 1)]
    )
    assert [row.action for row in actions][:2] == ["access.elevation.requested", "access.elevation.approved"]
