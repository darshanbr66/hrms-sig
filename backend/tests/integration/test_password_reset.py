"""Password reset (AUTH-5, threat model T16): anti-enumeration, single use, expiry, races, MFA kept."""

import asyncio
import hashlib
from datetime import timedelta

import httpx

from app.modules.identity.models import UserStatus
from app.platform.security.emails import EmailHasher
from tests.api_support import EMAIL_LOOKUP_KEY, PASSWORD, Account, Api, link_token, refresh_cookie

REQUEST = "/api/v1/auth/password-reset"
NEW_PASSWORD = "a brand new passphrase 2031"


async def request_reset(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post(REQUEST, json={"email": email})


async def reset_link(api: Api, account: Account) -> str:
    async with api.client() as client:
        assert (await request_reset(client, account.email)).status_code == 202
    [email] = [m for m in await api.deliver() if m.to == account.email and "/password-reset/" in m.body]
    return link_token(email)


async def complete(client: httpx.AsyncClient, token: str, password: str = NEW_PASSWORD) -> httpx.Response:
    return await client.post(f"{REQUEST}/{token}", json={"password": password})


async def test_the_answer_never_reveals_whether_an_account_exists(api: Api) -> None:
    active = await api.create_account()
    disabled = await api.create_account(status=UserStatus.DISABLED)
    invited = await api.create_account(status=UserStatus.INVITED)
    async with api.client() as client:
        responses = [
            await request_reset(client, email)
            for email in ("nobody-at-all@dev.example", active.email, disabled.email, invited.email)
        ]
    assert {r.status_code for r in responses} == {202}
    # Identical bodies, whatever the account's state.
    assert len({r.content for r in responses}) == 1
    sent = await api.deliver()
    assert [m.to for m in sent if m.to in {active.email, disabled.email, invited.email}] == [active.email]


async def test_unknown_addresses_are_recorded_only_as_a_keyed_hash(api: Api) -> None:
    unknown = "Nobody-Here@Dev.Example"
    async with api.client() as client:
        await request_reset(client, unknown)
    rows = await api.fetch(
        "SELECT email_attempted_hash, to_jsonb(e)::text FROM audit.security_events e "
        "WHERE event_type = 'password_reset.requested' AND email_attempted_hash = :h",
        h=EmailHasher(EMAIL_LOOKUP_KEY).digest(unknown),
    )
    assert len(rows) == 1
    digest, text = rows[0]
    assert "nobody-here" not in text.lower()
    assert bytes(digest) != hashlib.sha256(b"nobody-here@dev.example").digest()


async def test_requests_are_limited_per_email(api: Api) -> None:
    account = await api.create_account()
    for _ in range(3):
        async with api.client() as client:
            assert (await request_reset(client, account.email)).status_code == 202
    async with api.client() as client:
        # Same limit for an unknown address: the 429 says nothing about the account.
        limited = await request_reset(client, account.email.upper())
        assert limited.status_code == 429
    # Only the newest waiting email is kept, so one reset email goes out.
    await api.deliver()
    assert len(api.mail_to(account.email)) == 1


async def test_the_link_token_exists_only_in_the_email_and_as_a_hash(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await request_reset(client, account.email)
    stored = await api.fetch(
        "SELECT to_jsonb(o)::text FROM notify.email_outbox o WHERE user_id = :id", id=account.user_id
    )
    token = await reset_link(api, account)
    assert all(token not in row[0] for row in stored)
    rows = await api.fetch(
        "SELECT token_hash FROM identity.one_time_tokens WHERE user_id = :id AND purpose = 'password_reset'",
        id=account.user_id,
    )
    assert all(token.encode() not in bytes(row[0]) for row in rows)


async def test_reset_ends_sessions_and_devices_but_keeps_mfa(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as signed_in:
        await api.sign_in(signed_in, account)
        stale_refresh = refresh_cookie(signed_in)
        token = await reset_link(api, account)
        async with api.client() as client:
            weak = await complete(client, token, "passwordpassword")
            assert weak.json()["errors"][0]["code"] == "password.breached"
            assert (await complete(client, token)).status_code == 204
            # Not signed in by the reset.
            assert client.cookies.get("__Host-sv_at") is None
        assert (await signed_in.get("/api/v1/me")).status_code == 401
        signed_in.cookies.set("__Secure-sv_rt", stale_refresh, path="/api/v1/auth/refresh")
        assert (await signed_in.post("/api/v1/auth/refresh")).status_code == 401
    [(revoked,)] = await api.fetch(
        "SELECT count(*) FROM identity.trusted_devices "
        "WHERE user_id = :id AND revoked_reason = 'password_reset'",
        id=account.user_id,
    )
    assert revoked == 1
    async with api.client() as client:
        old = await client.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        assert old.status_code == 401
        # The new password alone is not enough: the second factor is still required.
        new = await client.post("/api/v1/auth/login", json={"email": account.email, "password": NEW_PASSWORD})
        assert set(new.json()) == {"mfa_token"}
    changed = [m for m in await api.deliver() if m.to == account.email]
    assert any("password" in m.subject.lower() and "changed" in m.subject.lower() for m in changed)


async def test_a_link_works_once(api: Api) -> None:
    account = await api.create_account()
    token = await reset_link(api, account)
    async with api.client() as client:
        assert (await complete(client, token)).status_code == 204
        replay = await complete(client, token, "yet another passphrase 99")
        assert replay.status_code == 409
        assert replay.json()["code"] == "password_reset.invalid"


async def test_a_link_expires(api: Api) -> None:
    account = await api.create_account()
    token = await reset_link(api, account)
    api.clock.advance(timedelta(minutes=30, seconds=1))
    async with api.client() as client:
        assert (await complete(client, token)).json()["code"] == "password_reset.invalid"


async def test_a_newer_link_replaces_the_older_one(api: Api) -> None:
    account = await api.create_account()
    first = await reset_link(api, account)
    second = await reset_link(api, account)
    async with api.client() as client:
        assert (await complete(client, first)).status_code == 409
        assert (await complete(client, second)).status_code == 204


async def test_simultaneous_completions_use_the_link_once(api: Api) -> None:
    account = await api.create_account()
    token = await reset_link(api, account)
    async with api.client() as one, api.client() as two:
        results = await asyncio.gather(
            complete(one, token, "first racing passphrase 1"),
            complete(two, token, "second racing passphrase 2"),
        )
    assert sorted(r.status_code for r in results) == [204, 409]


async def test_a_reset_ends_a_sign_in_in_progress(api: Api) -> None:
    """A password step passed before the reset cannot be finished after it."""
    account = await api.create_account()
    async with api.client() as attacker:
        challenge = (
            await attacker.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        ).json()["mfa_token"]
        token = await reset_link(api, account)
        async with api.client() as owner:
            assert (await complete(owner, token)).status_code == 204
        api.clock.advance(timedelta(seconds=30))
        finished = await attacker.post(
            "/api/v1/auth/login/mfa", json={"mfa_token": challenge, "code": account.code(api.clock.now())}
        )
        assert finished.json()["code"] == "login.challenge_expired"


async def test_a_disabled_account_cannot_complete_a_reset(api: Api) -> None:
    account = await api.create_account()
    token = await reset_link(api, account)
    await api.execute("UPDATE identity.users SET status = 'disabled' WHERE id = :id", id=account.user_id)
    async with api.client() as client:
        assert (await complete(client, token)).json()["code"] == "password_reset.invalid"


async def test_malformed_links_are_refused_cheaply(api: Api) -> None:
    async with api.client() as client:
        for token in ("x", "A" * 43, "%" * 43):
            assert (await complete(client, token)).status_code == 409
