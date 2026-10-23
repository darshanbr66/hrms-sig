"""Linking an existing account to an employee record (docs/security-architecture.md §3.1,
`POST /users/{id}/employee-link`). All data is obviously fake (docs/database-design.md §10)."""

import asyncio
import json
import uuid
from datetime import timedelta

from app.modules.identity.models import User
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.integration.test_dashboard_access import EXPECTED
from tests.people_data import add_job, create_employee, create_org_units

REASON = "Test: invited before the record existed"


async def employee_for(api: Api, account: Account) -> uuid.UUID:
    """An employee record whose work email is the account's email, as linking requires."""
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(session, units, employee, manager_id=None, effective_from=today - timedelta(days=30))
    assert await api.update("employees", {"_id": employee}, {"work_email": account.email}) == 1
    return employee


def link(employee: uuid.UUID, reason: str = REASON) -> dict[str, str]:
    return {"employee_id": str(employee), "reason": reason}


async def test_hr_links_an_account_with_step_up_and_a_reason_and_it_is_audited(api: Api) -> None:
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    target = await api.create_account()
    employee = await employee_for(api, target)
    path = f"/api/v1/users/{target.user_id}/employee-link"
    async with api.client() as client:
        await api.sign_in(client, hr)
        no_step_up = await client.post(path, json=link(employee))
        assert no_step_up.status_code == 403
        assert no_step_up.json()["type"].endswith("step-up-required")
        await api.step_up(client, hr)
        assert (await client.post(path, json={"employee_id": str(employee)})).status_code == 422
        linked = await client.post(path, json=link(employee))
        assert linked.status_code == 200, linked.text
        assert linked.json()["employee_id"] == str(employee)
        # Repeating the same link changes nothing and is not audited twice.
        again = await client.post(path, json=link(employee))
        assert again.status_code == 200

    rows = await api.records(
        AuditLogRecord, {"target_id": str(target.user_id), "action": "identity.user.employee_linked"}
    )
    assert len(rows) == 1
    permission, subject, reason, changes = (
        rows[0].permission_used,
        rows[0].subject_employee_id,
        rows[0].reason,
        json.dumps(rows[0].changes),
    )
    assert (permission, subject, reason) == ("user.invite", employee, REASON)
    assert target.email not in changes

    # The account now has the employee's self-service access.
    async with api.client() as client:
        await api.sign_in(client, target)
        me = (await client.get("/api/v1/me")).json()
        assert me["employee_id"] == str(employee)
        assert (await client.get("/api/v1/me/profile")).status_code == 200


async def test_an_account_invited_without_a_record_gets_the_employee_dashboard_once_linked(api: Api) -> None:
    """An account invited from the user list has no employee record: no self-service data (the
    dashboard says the account is not linked). Linked by HR, the same account loads every source
    of the employee dashboard."""
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    target = await api.create_account()
    employee = await employee_for(api, target)
    async with api.client() as client:
        await api.sign_in(client, target)
        assert (await client.get("/api/v1/me")).json()["employee_id"] is None
        unlinked = {path: (await client.get(path)).status_code for path in EXPECTED["employee"]}
    assert unlinked["/api/v1/attendance/today"] != 200
    assert unlinked["/api/v1/me/leave/balances"] != 200

    async with api.client() as client:
        await api.sign_in(client, hr)
        await api.step_up(client, hr)
        linked = await client.post(f"/api/v1/users/{target.user_id}/employee-link", json=link(employee))
        assert linked.status_code == 200, linked.text

    async with api.client() as client:
        await api.sign_in(client, target)
        assert (await client.get("/api/v1/me")).json()["employee_id"] == str(employee)
        got = {path: (await client.get(path)).status_code for path in EXPECTED["employee"]}
        assert (await client.get("/api/v1/me/profile")).status_code == 200
    assert got == EXPECTED["employee"]


async def test_linking_is_refused_without_both_permissions(api: Api) -> None:
    target = await api.create_account()
    employee = await employee_for(api, target)
    path = f"/api/v1/users/{target.user_id}/employee-link"
    for roles in ((RoleKey.SYSTEM_ADMIN.value,), (RoleKey.SUPER_ADMIN.value,), ()):
        actor = await api.create_account(roles=roles)
        async with api.client() as client:
            await api.sign_in(client, actor)
            await api.step_up(client, actor)
            refused = await client.post(path, json=link(employee))
            assert refused.status_code in (403, 404), (roles, refused.text)
    # HR restricted to another department does not learn the employee exists.
    async with api.database.unit_of_work() as session:
        other = await create_org_units(session)
    narrow = await api.create_account(
        roles=(RoleKey.HR.value,), role_scope={"department_id": other.department_id}
    )
    async with api.client() as client:
        await api.sign_in(client, narrow)
        await api.step_up(client, narrow)
        assert (await client.post(path, json=link(employee))).status_code == 404
    assert [row.employee_id for row in await api.records(User, {"id": target.user_id})] == [None]


async def test_conflicting_links_are_refused(api: Api) -> None:
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    first = await api.create_account()
    employee = await employee_for(api, first)
    other_employee = await employee_for(api, await api.create_account())
    linked_elsewhere = await api.create_account(employee_id=other_employee)
    admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    admin_employee = await employee_for(api, admin)
    wrong_email = await api.create_account()
    free_employee = await employee_for(api, await api.create_account())
    disabled = await api.create_account()
    disabled_employee = await employee_for(api, disabled)
    assert await api.update("users", {"_id": disabled.user_id}, {"status": "disabled"}) == 1

    async with api.client() as client:
        await api.sign_in(client, hr)
        await api.step_up(client, hr)

        async def attempt(account: uuid.UUID, target: uuid.UUID) -> tuple[int, str | None]:
            response = await client.post(f"/api/v1/users/{account}/employee-link", json=link(target))
            return response.status_code, response.json().get("code")

        assert await attempt(first.user_id, employee) == (200, None)
        # The employee already has an account.
        assert await attempt(wrong_email.user_id, employee) == (409, "user.employee_has_account")
        # The account already belongs to another employee.
        assert await attempt(linked_elsewhere.user_id, employee) == (409, "user.already_linked")
        # The account's email is not the employee's work email.
        assert await attempt(wrong_email.user_id, free_employee) == (409, "user.email_not_work_email")
        # An administrator account may be linked to its holder's own record (owner decision,
        # 2026-10-23); test_admin_employee_link.py covers what it may then do.
        assert await attempt(admin.user_id, admin_employee) == (200, None)
        assert await attempt(disabled.user_id, disabled_employee) == (409, "user.not_active")
        assert (await attempt(uuid.uuid4(), employee))[0] == 404
        assert (await attempt(wrong_email.user_id, uuid.uuid4()))[0] == 404
        # Never one's own account.
        own = await client.post(f"/api/v1/users/{hr.user_id}/employee-link", json=link(employee))
        assert own.status_code == 403


async def test_two_accounts_racing_for_one_employee_leave_one_link(api: Api) -> None:
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    one = await api.create_account()
    employee = await employee_for(api, one)
    two = await api.create_account()
    # Both accounts carry the work email in turn would be impossible (emails are unique), so the
    # race is the same account linked twice at once: one change and one audit row. The second
    # request either finds the link made (200) or collides with it and is told to retry (409
    # concurrent_change: MongoDB fails the second writer instead of making it wait).
    async with api.client() as a, api.client() as b:
        for client in (a, b):
            await api.sign_in(client, hr)
            await api.step_up(client, hr)
        responses = await asyncio.gather(
            a.post(f"/api/v1/users/{one.user_id}/employee-link", json=link(employee)),
            b.post(f"/api/v1/users/{one.user_id}/employee-link", json=link(employee)),
        )
        outcomes = sorted((r.status_code, r.json().get("code")) for r in responses)
        assert outcomes in ([(200, None), (200, None)], [(200, None), (409, "concurrent_change")])
        late = await a.post(f"/api/v1/users/{two.user_id}/employee-link", json=link(employee))
        assert late.status_code == 409
    query = {"target_id": str(one.user_id), "action": "identity.user.employee_linked"}
    assert await api.count("audit_log", query) == 1
