"""Inviting people (security-architecture §3.1): authority, employee links, single use, resend, revoke."""

import asyncio
import uuid
from datetime import timedelta

import httpx
import pyotp
import pytest

from app.platform.authz.roles import RoleKey
from app.platform.security import passwords
from tests.api_support import PASSWORD, Account, Api, link_token
from tests.people_data import add_job, create_employee, create_org_units


async def admin(api: Api, client: httpx.AsyncClient, role: RoleKey) -> Account:
    account = await api.create_account(roles=(role.value,))
    await api.sign_in(client, account)
    return account


async def invite_link(api: Api, email: str) -> str:
    [message] = [m for m in await api.deliver() if m.to == email and "/invite/" in m.body]
    return link_token(message)


async def activate(api: Api, client: httpx.AsyncClient, token: str) -> httpx.Response:
    base = f"/api/v1/auth/invite/{token}"
    password = await client.post(f"{base}/password", json={"password": PASSWORD})
    if password.status_code != 200:
        return password
    enrolment = password.json()["enrolment_token"]
    setup_response = await client.post(f"{base}/mfa/totp/setup", json={"enrolment_token": enrolment})
    if setup_response.status_code != 200:
        return setup_response
    setup = setup_response.json()
    return await client.post(
        f"{base}/mfa/totp/confirm",
        json={
            "enrolment_token": enrolment,
            "factor_id": setup["factor_id"],
            "code": pyotp.TOTP(setup["secret"]).at(api.clock.now()),
        },
    )


def fresh_email() -> str:
    return f"invitee-{uuid.uuid4().hex[:10]}@dev.example"


async def test_an_invite_is_emailed_and_activates_once(api: Api) -> None:
    email = fresh_email()
    async with api.client() as client:
        inviter = await admin(api, client, RoleKey.SYSTEM_ADMIN)
        created = await client.post("/api/v1/users", json={"email": email.upper()})
        assert created.status_code == 201
        assert created.json()["status"] == "invited"
        assert created.json()["email"] == email  # stored in canonical form
        user_id = created.json()["id"]
    token = await invite_link(api, email)
    async with api.client() as browser:
        activated = await activate(api, browser, token)
        assert activated.status_code == 200, activated.text
        # The activating browser becomes a trusted device; it is not reported as new.
        assert browser.cookies.get("__Host-sv_dev")
        assert (await browser.get("/api/v1/me")).json()["roles"] == []  # an invite grants no role
    assert [m for m in await api.deliver() if m.to == email] == []
    async with api.client() as again:
        assert (await again.get(f"/api/v1/auth/invite/{token}")).json()["valid"] is False
    rows = await api.fetch(
        "SELECT action, permission_used FROM audit.audit_log WHERE target_id = :id ORDER BY recorded_at, id",
        id=user_id,
    )
    assert rows[0] == ("identity.user.invited", "user.invite")
    assert ("identity.user.activated", None) in rows
    assert inviter.user_id


async def test_two_activations_of_one_invite_produce_one_account(api: Api) -> None:
    email = fresh_email()
    async with api.client() as client:
        await admin(api, client, RoleKey.HR)
        await client.post("/api/v1/users", json={"email": email})
    token = await invite_link(api, email)
    async with api.client() as one, api.client() as two:
        results = await asyncio.gather(activate(api, one, token), activate(api, two, token))
    assert sorted(r.status_code for r in results)[0] == 200
    assert sum(r.status_code == 200 for r in results) == 1


async def test_resend_and_revoke_retire_the_outstanding_link_at_once(api: Api) -> None:
    email = fresh_email()
    async with api.client() as client:
        await admin(api, client, RoleKey.HR)
        user_id = (await client.post("/api/v1/users", json={"email": email})).json()["id"]
        first = await invite_link(api, email)
        assert (await client.post(f"/api/v1/users/{user_id}/invite")).status_code == 202
        async with api.client() as browser:
            assert (await browser.get(f"/api/v1/auth/invite/{first}")).json()["valid"] is False
        second = await invite_link(api, email)
        assert (await client.delete(f"/api/v1/users/{user_id}/invite")).status_code == 204
        async with api.client() as browser:
            assert (await activate(api, browser, second)).json()["code"] == "invite.invalid"
        # Revoking also cancels an invite not yet sent.
        await client.post(f"/api/v1/users/{user_id}/invite")
        await client.delete(f"/api/v1/users/{user_id}/invite")
    assert [m for m in await api.deliver() if m.to == email] == []


async def test_an_invite_expires(api: Api) -> None:
    email = fresh_email()
    async with api.client() as client:
        await admin(api, client, RoleKey.HR)
        await client.post("/api/v1/users", json={"email": email})
    token = await invite_link(api, email)
    api.clock.advance(timedelta(hours=72, seconds=1))
    async with api.client() as browser:
        assert (await activate(api, browser, token)).json()["code"] == "invite.invalid"


async def test_only_inviters_can_invite_and_existing_accounts_are_reported_to_them(api: Api) -> None:
    existing = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, await api.create_account(roles=(RoleKey.AUDITOR.value,)))
        assert (await client.post("/api/v1/users", json={"email": fresh_email()})).status_code == 403
    async with api.client() as client:
        await admin(api, client, RoleKey.HR)
        taken = await client.post("/api/v1/users", json={"email": existing.email.upper()})
        assert taken.json()["code"] == "user.email_in_use"
        active = await client.post(f"/api/v1/users/{existing.user_id}/invite")
        assert active.json()["code"] == "user.not_invited"


async def test_linking_an_employee_needs_hr_authority_and_their_work_email(api: Api) -> None:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee = await create_employee(session, joined=today)
        await add_job(session, units, employee, manager_id=None, effective_from=today)
    [(work_email,)] = await api.fetch("SELECT work_email FROM people.employees WHERE id = :id", id=employee)

    # A system admin has user.invite but no authority over employee records.
    async with api.client() as client:
        await admin(api, client, RoleKey.SYSTEM_ADMIN)
        refused = await client.post("/api/v1/users", json={"email": work_email, "employee_id": str(employee)})
        assert refused.status_code == 404
    async with api.client() as client:
        await admin(api, client, RoleKey.HR)
        # Routing the employee's account to another mailbox is refused.
        elsewhere = await client.post(
            "/api/v1/users", json={"email": fresh_email(), "employee_id": str(employee)}
        )
        assert elsewhere.json()["errors"][0]["code"] == "user.email_not_work_email"
        missing = await client.post(
            "/api/v1/users", json={"email": fresh_email(), "employee_id": str(uuid.uuid4())}
        )
        assert missing.status_code == 404
        linked = await client.post("/api/v1/users", json={"email": work_email, "employee_id": str(employee)})
        assert linked.status_code == 201
        assert linked.json()["employee_id"] == str(employee)
        twice = await client.post(
            "/api/v1/users", json={"email": f"x{work_email}", "employee_id": str(employee)}
        )
        assert twice.status_code in (409, 422)


async def test_disabling_an_invited_account_stops_its_link(api: Api) -> None:
    email = fresh_email()
    async with api.client() as client:
        hr = await admin(api, client, RoleKey.HR)
        user_id = (await client.post("/api/v1/users", json={"email": email})).json()["id"]
        token = await invite_link(api, email)
        await api.step_up(client, hr)
        assert (await client.post(f"/api/v1/users/{user_id}/disable")).status_code == 200
    async with api.client() as browser:
        assert (await browser.get(f"/api/v1/auth/invite/{token}")).json()["valid"] is False


async def test_an_invalid_invite_link_never_costs_a_password_hash(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the password step hashed before checking the link, so garbage links could
    keep the limited Argon2 slots busy and slow down sign-in for everyone."""
    hashed: list[str] = []

    async def counting_hash(password: str) -> str:
        hashed.append(password)
        return "never used"

    monkeypatch.setattr(passwords, "hash_password", counting_hash)
    async with api.client() as client:
        response = await client.post(f"/api/v1/auth/invite/{'Z' * 43}/password", json={"password": PASSWORD})
    assert response.json()["code"] == "invite.invalid"
    assert hashed == []
