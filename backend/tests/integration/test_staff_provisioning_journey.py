"""Provisioning HR staff accounts over HTTP, end to end (docs/roles-and-permissions.md, workflows
for HR and HR admin accounts): an account is invited without an employee record, activated with a
password and an authenticator, and only then given `hr` or `hr_admin` by a super admin, with
step-up and a reason. Every step is a supported, audited endpoint; no role row is written directly
for the accounts being provisioned.

All data is obviously fake (docs/database-design.md §10).
"""

import uuid
from datetime import timedelta

import httpx
import pyotp

from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from app.platform.security import totp
from tests.api_support import PASSWORD, Account, Api, fake_email, link_token
from tests.people_data import fake_code


async def invite(api: Api, client: httpx.AsyncClient, email: str) -> uuid.UUID:
    created = await client.post("/api/v1/users", json={"email": email})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "invited"
    return uuid.UUID(created.json()["id"])


async def invite_links(api: Api, *emails: str) -> dict[str, str]:
    """The invite link token sent to each address (the outbox delivers them together)."""
    sent = {m.to: link_token(m) for m in await api.deliver() if m.to in emails and "/invite/" in m.body}
    assert set(sent) == set(emails)
    return sent


async def activate(api: Api, browser: httpx.AsyncClient, token: str) -> str:
    """Password, then an authenticator; returns the authenticator secret, as the person's app
    would hold it, for later step-up."""
    base = f"/api/v1/auth/invite/{token}"
    password = await browser.post(f"{base}/password", json={"password": PASSWORD})
    assert password.status_code == 200, password.text
    enrolment = password.json()["enrolment_token"]
    setup = (await browser.post(f"{base}/mfa/totp/setup", json={"enrolment_token": enrolment})).json()
    confirmed = await browser.post(
        f"{base}/mfa/totp/confirm",
        json={
            "enrolment_token": enrolment,
            "factor_id": setup["factor_id"],
            "code": pyotp.TOTP(setup["secret"]).at(api.clock.now()),
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    secret: str = setup["secret"]
    return secret


async def me(browser: httpx.AsyncClient) -> dict[str, object]:
    # A role change ends the session's access token; the browser refreshes as the SPA does.
    if (response := await browser.get("/api/v1/me")).status_code == 401:
        assert (await browser.post("/api/v1/auth/refresh")).status_code == 200
        response = await browser.get("/api/v1/me")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_hr_and_hr_admin_accounts_are_invited_activated_and_then_given_their_roles(  # noqa: PLR0915 (one workflow end to end)
    api: Api,
) -> None:
    super_admin: Account = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    system_admin: Account = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    hr_admin_email, hr_email = fake_email(), fake_email()

    # A super admin has no `user.invite`: inviting is an account administrator's job.
    async with api.client() as client:
        await api.sign_in(client, super_admin)
        assert (await client.post("/api/v1/users", json={"email": hr_admin_email})).status_code == 403

    async with api.client() as client:
        await api.sign_in(client, system_admin)
        hr_admin_id = await invite(api, client, hr_admin_email)
        hr_id = await invite(api, client, hr_email)
        # A system admin can invite but not give roles.
        refused = await client.post(
            f"/api/v1/users/{hr_admin_id}/roles", json={"role_key": "hr_admin", "reason": "Test"}
        )
        assert refused.status_code == 403

    async with api.client() as hr_admin_browser, api.client() as hr_browser:
        links = await invite_links(api, hr_admin_email, hr_email)
        hr_admin_secret = await activate(api, hr_admin_browser, links[hr_admin_email])
        await activate(api, hr_browser, links[hr_email])
        # Activated, with an authenticator, and no role: nothing beyond their own account.
        assert (await me(hr_admin_browser))["roles"] == []
        assert (await hr_admin_browser.get("/api/v1/departments")).status_code == 403

        async with api.client() as client:
            await api.sign_in(client, super_admin)
            # Assigning a role needs step-up.
            no_step_up = await client.post(
                f"/api/v1/users/{hr_admin_id}/roles", json={"role_key": "hr_admin", "reason": "Test HR admin"}
            )
            assert no_step_up.status_code == 403
            assert no_step_up.json()["type"].endswith("step-up-required")
            await api.step_up(client, super_admin)
            for user_id, role in ((hr_admin_id, "hr_admin"), (hr_id, "hr")):
                assigned = await client.post(
                    f"/api/v1/users/{user_id}/roles",
                    json={"role_key": role, "reason": f"Test {role} account"},
                )
                assert assigned.status_code == 201, assigned.text
            # A super admin cannot give themselves an HR role (SOD-3) or hold one standing (SOD-10).
            own = await client.post(
                f"/api/v1/users/{super_admin.user_id}/roles", json={"role_key": "hr_admin", "reason": "Test"}
            )
            assert own.status_code in (403, 409)

        hr_admin_me = await me(hr_admin_browser)
        assert hr_admin_me["roles"] == ["hr_admin"]
        permissions = set(hr_admin_me["permissions"])  # type: ignore[call-overload]
        assert {"org.manage", "user.invite", "employee.create", "leave.policy.manage"} <= permissions
        assert "role.assign" not in permissions
        # An HR admin sets up the organization but cannot give anyone a role, themselves included.
        created = await hr_admin_browser.post(
            "/api/v1/departments", json={"code": fake_code(), "name": "Test"}
        )
        assert created.status_code == 201, created.text
        for target in (hr_id, hr_admin_id):
            denied = await hr_admin_browser.post(
                f"/api/v1/users/{target}/roles", json={"role_key": "hr_admin", "reason": "Test"}
            )
            assert denied.status_code == 403

        hr_me = await me(hr_browser)
        assert hr_me["roles"] == ["hr"]
        assert "org.manage" not in set(hr_me["permissions"])  # type: ignore[call-overload]
        refused_org = await hr_browser.post("/api/v1/departments", json={"code": fake_code(), "name": "Test"})
        assert refused_org.status_code == 403

        # Disabling needs step-up, and ends the account's sessions at once.
        assert (await hr_admin_browser.post(f"/api/v1/users/{hr_id}/disable")).status_code == 403
        api.clock.advance(timedelta(seconds=totp.STEP_SECONDS))
        stepped = await hr_admin_browser.post(
            "/api/v1/auth/step-up", json={"code": pyotp.TOTP(hr_admin_secret).at(api.clock.now())}
        )
        assert stepped.status_code == 200, stepped.text
        disabled = await hr_admin_browser.post(f"/api/v1/users/{hr_id}/disable")
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disabled"
        assert (await hr_browser.get("/api/v1/me")).status_code == 401
        assert (await hr_browser.post("/api/v1/auth/refresh")).status_code == 401

    rows = await api.records(
        AuditLogRecord,
        {"target_id": {"$in": [str(hr_admin_id), str(hr_id)]}},
        sort=[("recorded_at", 1), ("id", 1)],
    )
    actions = [row.action for row in rows]
    assert actions.count("identity.user.invited") == 2
    assert actions.count("identity.user.activated") == 2
    assigned_rows = [row for row in rows if row.action == "access.role.assigned"]
    assert [(row.permission_used, row.reason) for row in assigned_rows] == [
        ("role.assign", "Test hr_admin account"),
        ("role.assign", "Test hr account"),
    ]
    assert "identity.user.disabled" in actions
