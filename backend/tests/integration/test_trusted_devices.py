"""Trusted devices and security emails (security-architecture §3.3-3.4).

A trusted device only suppresses the new-device email. It never replaces the second factor.
"""

import asyncio
from datetime import timedelta

import httpx

from app.platform.authz.roles import RoleKey
from tests.api_support import PASSWORD, Account, Api

DEVICE = "__Host-sv_dev"


def device_cookie(client: httpx.AsyncClient) -> str:
    value = client.cookies.get(DEVICE)
    assert value
    return value


def new_device_mail(api: Api, account: Account) -> list[str]:
    return [m.subject for m in api.mail_to(account.email) if "New sign-in" in m.subject]


async def test_a_new_browser_is_reported_once_and_then_recognized(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as browser:
        signed_in = await api.sign_in(browser, account)
        cookie = next(c for c in signed_in.headers.get_list("set-cookie") if c.startswith(f"{DEVICE}="))
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "Path=/" in cookie
        await api.deliver()
        assert len(new_device_mail(api, account)) == 1
        await browser.post("/api/v1/auth/logout")
        await api.sign_in(browser, account)
        await api.deliver()
        assert len(new_device_mail(api, account)) == 1
        devices = (await browser.get("/api/v1/me/devices")).json()["items"]
        assert [d["current"] for d in devices] == [True]
        token = device_cookie(browser)
    stored = await api.fetch(
        "SELECT token_hash FROM identity.trusted_devices WHERE user_id = :id", id=account.user_id
    )
    assert all(token.encode() not in bytes(row[0]) for row in stored)


async def test_a_trusted_device_never_skips_the_second_factor(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as browser:
        await api.sign_in(browser, account)
        await browser.post("/api/v1/auth/logout")
        login = await browser.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        assert set(login.json()) == {"mfa_token"}
        assert "set-cookie" not in login.headers


async def test_revoking_a_device_makes_its_next_sign_in_new(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as laptop, api.client() as phone:
        await api.sign_in(laptop, account)
        await api.sign_in(phone, account)
        devices = (await laptop.get("/api/v1/me/devices")).json()["items"]
        phone_device = next(d["id"] for d in devices if not d["current"])
        assert (await laptop.delete(f"/api/v1/me/devices/{phone_device}")).status_code == 204
        await api.deliver()
        before = len(new_device_mail(api, account))
        await phone.post("/api/v1/auth/logout")
        await api.sign_in(phone, account)
        await api.deliver()
        assert len(new_device_mail(api, account)) == before + 1


async def test_a_device_expires(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as browser:
        await api.sign_in(browser, account)
        await api.deliver()
        api.clock.advance(timedelta(days=90, minutes=1))
        await api.sign_in(browser, account)
        await api.deliver()
        assert len(new_device_mail(api, account)) == 2


async def test_a_user_cannot_revoke_another_users_device(api: Api) -> None:
    alice, bob = await api.create_account(), await api.create_account()
    async with api.client() as alice_browser, api.client() as bob_browser:
        await api.sign_in(alice_browser, alice)
        await api.sign_in(bob_browser, bob)
        [bob_device] = (await bob_browser.get("/api/v1/me/devices")).json()["items"]
        assert (await alice_browser.delete(f"/api/v1/me/devices/{bob_device['id']}")).status_code == 404


async def test_password_and_mfa_changes_keep_only_the_current_device(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as current, api.client() as other:
        await api.sign_in(current, account)
        await api.sign_in(other, account)
        await api.step_up(current, account)
        assert (await current.post("/api/v1/me/mfa/recovery-codes")).status_code == 200
        devices = (await current.get("/api/v1/me/devices")).json()["items"]
        assert [d["current"] for d in devices] == [True]
    rows = await api.fetch(
        "SELECT revoked_reason FROM identity.trusted_devices WHERE user_id = :id AND revoked_at IS NOT NULL",
        id=account.user_id,
    )
    assert rows == [("mfa_changed",)]
    sent = [m.subject for m in await api.deliver() if m.to == account.email]
    assert any("sign-in settings were changed" in subject for subject in sent)


async def test_an_admin_session_revocation_also_ends_device_trust(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as browser:
        await api.sign_in(browser, account)
    async with api.client() as admin_client:
        admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
        await api.sign_in(admin_client, admin)
        await api.step_up(admin_client, admin)
        assert (await admin_client.delete(f"/api/v1/users/{account.user_id}/sessions")).status_code == 200
    rows = await api.fetch(
        "SELECT revoked_reason FROM identity.trusted_devices WHERE user_id = :id", id=account.user_id
    )
    assert rows == [("revoked_by_admin",)]


async def test_simultaneous_sign_ins_with_one_cookie_report_nothing_new(api: Api) -> None:
    """Both completions lock the same device row; neither creates a second trust or email."""
    account = await api.create_account()
    async with api.client() as browser:
        await api.sign_in(browser, account)
        cookie = device_cookie(browser)
    await api.deliver()
    async with api.client() as one, api.client() as two:
        challenges = []
        for client in (one, two):
            client.cookies.set(DEVICE, cookie)
            login = await client.post(
                "/api/v1/auth/login", json={"email": account.email, "password": PASSWORD}
            )
            challenges.append(login.json())
        api.clock.advance(timedelta(seconds=30))
        # Codes from two adjacent steps, both within tolerance, so replay protection allows both.
        now = api.clock.now()
        results = await asyncio.gather(
            one.post("/api/v1/auth/login/mfa", json={**challenges[0], "code": account.code(now)}),
            two.post(
                "/api/v1/auth/login/mfa",
                json={**challenges[1], "code": account.code(now + timedelta(seconds=30))},
            ),
        )
        assert any(result.status_code == 200 for result in results)
    await api.deliver()
    assert len(new_device_mail(api, account)) == 1
    rows = await api.fetch(
        "SELECT count(*) FROM identity.trusted_devices WHERE user_id = :id AND revoked_at IS NULL",
        id=account.user_id,
    )
    assert rows == [(1,)]


async def test_a_lockout_sends_one_email_however_many_attempts_follow(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        for _ in range(14):
            await client.post(
                "/api/v1/auth/login", json={"email": account.email, "password": "a wrong passphrase"}
            )
    await api.deliver()
    locked = [m for m in api.mail_to(account.email) if "temporarily locked" in m.subject]
    assert len(locked) == 1
    assert "15 minutes" in locked[0].body


async def test_a_recovery_code_sign_in_is_reported(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.step_up(client, account)
        codes = (await client.post("/api/v1/me/mfa/recovery-codes")).json()["codes"]
    async with api.client() as client:
        token = (
            await client.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        ).json()
        await client.post("/api/v1/auth/login/mfa", json={**token, "recovery_code": codes[0]})
    await api.deliver()
    [used] = [m for m in api.mail_to(account.email) if "recovery code was used" in m.subject]
    assert "9 unused recovery codes" in used.body
    assert codes[1] not in used.body
