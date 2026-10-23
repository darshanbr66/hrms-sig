"""Enabling a disabled account again (docs/security-architecture.md §3.1, roles-and-permissions §6).

Disabling alone is reversible: someone holding `user.disable` (with step-up) enables the account
again. Two states are not reversible through that action, whoever asks, and the account view
says which (`enable_blocked`) so the page shows a status instead of a button: the linked
employee has left (`409 user.employee_exited`; there is no rehire workflow), and the person's
data was permanently erased (`409 user.erased`; never, by any workflow, and the account is
never linked to anyone again). All data is obviously fake (docs/database-design.md §10).
"""

from datetime import timedelta

import httpx

from app.modules.identity.models import User, UserStatus
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.integration.test_account_links import employee_for, link
from tests.people_data import add_job, create_employee, create_org_units


async def signed_in(api: Api, client: httpx.AsyncClient, account: Account) -> None:
    await api.sign_in(client, account)
    await api.step_up(client, account)


async def linked_employee(api: Api) -> Account:
    """An active account linked to an employee employed today."""
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=today - timedelta(days=90))
        await add_job(session, units, employee_id, manager_id=None, effective_from=today - timedelta(days=90))
    return await api.create_account(employee_id=employee_id)


async def view(client: httpx.AsyncClient, account: Account) -> dict[str, object]:
    response = await client.get(f"/api/v1/users/{account.user_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_an_ordinarily_disabled_account_is_enabled_again(api: Api) -> None:
    target = await linked_employee(api)
    unlinked = await api.create_account()
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await signed_in(api, client, hr)
        for account in (target, unlinked):
            disabled = await client.post(f"/api/v1/users/{account.user_id}/disable")
            assert disabled.status_code == 200, disabled.text
            # Disabled, and nothing makes it final: the page offers Enable account.
            assert (disabled.json()["status"], disabled.json()["enable_blocked"]) == ("disabled", None)
            assert (await view(client, account))["enable_blocked"] is None
            enabled = await client.post(f"/api/v1/users/{account.user_id}/enable")
            assert enabled.status_code == 200, enabled.text
            assert (enabled.json()["status"], enabled.json()["enable_blocked"]) == ("active", None)
    # The person signs in again with their own password and authenticator.
    async with api.client() as client:
        await api.sign_in(client, target)
        assert (await client.get("/api/v1/me")).status_code == 200
    actions = [
        row.action
        for row in await api.records(
            AuditLogRecord, {"target_id": str(target.user_id)}, sort=[("recorded_at", 1), ("id", 1)]
        )
    ]
    assert actions[-2:] == ["identity.user.disabled", "identity.user.enabled"]


async def test_enabling_needs_user_disable_and_step_up(api: Api) -> None:
    target = await api.create_account()
    assert await api.update("users", {"_id": target.user_id}, {"status": "disabled"}) == 1
    for roles in ((), (RoleKey.SUPER_ADMIN.value,), (RoleKey.AUDITOR.value,), (RoleKey.PAYROLL_ADMIN.value,)):
        actor = await api.create_account(roles=roles)
        async with api.client() as client:
            await signed_in(api, client, actor)
            refused = await client.post(f"/api/v1/users/{target.user_id}/enable")
            assert refused.status_code == 403, roles
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, hr)
        no_step_up = await client.post(f"/api/v1/users/{target.user_id}/enable")
        assert no_step_up.json()["type"] == "/problems/step-up-required"
    assert [user.status for user in await api.records(User, {"id": target.user_id})] == ["disabled"]


async def test_an_offboarded_persons_account_stays_disabled(api: Api) -> None:
    target = await linked_employee(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    system_admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    async with api.client() as client:
        await signed_in(api, client, hr)
        left = await client.post(
            f"/api/v1/employees/{target.employee_id}/exit",
            json={"last_working_day": api.clock.now().date().isoformat(), "reason": "Test: resigned"},
        )
        assert (left.status_code, left.json()["account"]) == (201, "disabled")
        assert (await view(client, target))["enable_blocked"] == "employee_exited"
        # The account list leaves leavers to the former employees list; asked for, it says why.
        assert (await client.get("/api/v1/users", params={"q": target.email})).json()["items"] == []
        listed = await client.get("/api/v1/users", params={"q": target.email, "include_offboarded": "true"})
        assert [item["enable_blocked"] for item in listed.json()["items"]] == ["employee_exited"]
    # Whoever asks, the ordinary action does not reinstate a leaver.
    for actor in (hr, system_admin):
        async with api.client() as client:
            await signed_in(api, client, actor)
            refused = await client.post(f"/api/v1/users/{target.user_id}/enable")
            assert (refused.status_code, refused.json()["code"]) == (409, "user.employee_exited")
            assert "has left" in refused.json()["detail"]
    assert [user.status for user in await api.records(User, {"id": target.user_id})] == ["disabled"]
    assert (
        await api.count("audit_log", {"target_id": str(target.user_id), "action": "identity.user.enabled"})
        == 0
    )


async def test_an_erased_account_is_never_enabled_or_linked_again(api: Api) -> None:
    """The erased state as the erasure leaves it (test_employee_erasure.py runs the whole
    erasure); here only the account rules."""
    target = await linked_employee(api)
    erased_email = f"erased-{target.user_id.hex}@erased.invalid"
    assert (
        await api.update(
            "users",
            {"_id": target.user_id},
            {"status": "disabled", "email": erased_email, "erased_at": api.clock.now()},
        )
        == 1
    )
    system_admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await signed_in(api, client, system_admin)
        assert (await view(client, target))["enable_blocked"] == "erased"
        refused = await client.post(f"/api/v1/users/{target.user_id}/enable")
        assert (refused.status_code, refused.json()["code"]) == (409, "user.erased")
        # Disabling it again changes nothing.
        assert (await client.post(f"/api/v1/users/{target.user_id}/disable")).json()["status"] == "disabled"
    # Never linked to another person's record, so never restored or reused.
    other = await employee_for(api, await api.create_account())
    async with api.client() as client:
        await signed_in(api, client, hr)
        relinked = await client.post(f"/api/v1/users/{target.user_id}/employee-link", json=link(other))
        assert (relinked.status_code, relinked.json()["code"]) == (409, "user.erased")
    [user] = await api.records(User, {"id": target.user_id})
    assert (user.status, user.email, user.employee_id) == ("disabled", erased_email, target.employee_id)


async def test_an_invited_account_that_was_disabled_goes_back_to_its_invite(api: Api) -> None:
    invited = await api.create_account(status=UserStatus.INVITED)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await signed_in(api, client, hr)
        assert (await client.post(f"/api/v1/users/{invited.user_id}/disable")).json()["status"] == "disabled"
        enabled = await client.post(f"/api/v1/users/{invited.user_id}/enable")
        assert (enabled.status_code, enabled.json()["status"]) == (200, "invited")
