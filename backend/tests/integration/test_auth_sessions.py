"""Server-side sessions: rotation, reuse detection, revocation, expiry (security-architecture §3.5)."""

import asyncio
from datetime import timedelta

import httpx

from app.modules.identity.models import UserStatus
from tests.api_support import Api, refresh_cookie

REFRESH = "/api/v1/auth/refresh"


def access_cookie(client: httpx.AsyncClient) -> str:
    return client.cookies.get("__Host-sv_at") or ""


async def test_tokens_are_stored_only_as_digests(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        access, refresh = access_cookie(client), refresh_cookie(client)
    rows = await api.fetch(
        "SELECT encode(t.token_hash, 'escape') FROM identity.session_tokens t "
        "JOIN identity.sessions s ON s.id = t.session_id WHERE s.user_id = :id",
        id=account.user_id,
    )
    stored = " ".join(row[0] for row in rows)
    assert access not in stored
    assert refresh not in stored


async def test_refresh_rotates_both_tokens_and_ends_the_old_ones(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        old_access, old_refresh = access_cookie(client), refresh_cookie(client)
        rotated = await client.post(REFRESH)
        assert rotated.status_code == 200
        assert rotated.json() == {"session_scope": "full"}
        assert access_cookie(client) != old_access
        assert refresh_cookie(client) != old_refresh
        assert (await client.get("/api/v1/me")).status_code == 200

    async with api.client() as stale:
        stale.cookies.set("__Host-sv_at", old_access)
        assert (await stale.get("/api/v1/me")).status_code == 401


async def test_a_reused_refresh_token_revokes_the_whole_session(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        stolen = refresh_cookie(client)
        assert (await client.post(REFRESH)).status_code == 200
        # The thief presents the old refresh token.
        async with api.client() as thief:
            thief.cookies.set("__Secure-sv_rt", stolen, path="/api/v1/auth/refresh")
            assert (await thief.post(REFRESH)).status_code == 401
        # The legitimate client is signed out too: the session is revoked.
        assert (await client.get("/api/v1/me")).status_code == 401
        assert (await client.post(REFRESH)).status_code == 401
    events = await api.fetch(
        "SELECT severity FROM audit.security_events "
        "WHERE user_id = :id AND event_type = 'token.reuse_detected'",
        id=account.user_id,
    )
    assert events == [("high",)]
    [(reason,)] = await api.fetch(
        "SELECT revoked_reason FROM identity.sessions WHERE user_id = :id", id=account.user_id
    )
    assert reason == "token_reuse"


async def test_two_simultaneous_refreshes_with_one_token_are_treated_as_reuse(api: Api) -> None:
    """Single use is enforced under concurrency: one wins, the other is the reuse signal."""
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        token = refresh_cookie(client)
        async with api.client() as first, api.client() as second:
            for other in (first, second):
                other.cookies.set("__Secure-sv_rt", token, path="/api/v1/auth/refresh")
            results = await asyncio.gather(first.post(REFRESH), second.post(REFRESH))
        assert sorted(r.status_code for r in results) == [200, 401]
    [(revoked_at,)] = await api.fetch(
        "SELECT revoked_at FROM identity.sessions WHERE user_id = :id", id=account.user_id
    )
    assert revoked_at is not None


async def test_a_refresh_racing_a_sign_out_never_errors(api: Api) -> None:
    """Both lock the session row first, so they serialize instead of deadlocking."""
    for _ in range(5):
        account = await api.create_account()
        async with api.client() as client:
            await api.sign_in(client, account)
            results = await asyncio.gather(client.post(REFRESH), client.post("/api/v1/auth/logout"))
            assert all(r.status_code in (200, 204, 401) for r in results), [r.text for r in results]
        [(revoked_at,)] = await api.fetch(
            "SELECT revoked_at FROM identity.sessions WHERE user_id = :id", id=account.user_id
        )
        assert revoked_at is not None


async def test_logout_revokes_the_session_and_clears_cookies(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        token = access_cookie(client)
        response = await client.post("/api/v1/auth/logout")
        assert response.status_code == 204
        cleared = response.headers.get_list("set-cookie")
        assert any(c.startswith('__Host-sv_at="";') or "Max-Age=0" in c for c in cleared)
        assert (await client.post(REFRESH)).status_code == 401
    async with api.client() as replay:
        replay.cookies.set("__Host-sv_at", token)
        assert (await replay.get("/api/v1/me")).status_code == 401
    events = await api.fetch(
        "SELECT details->>'reason' FROM audit.security_events "
        "WHERE user_id = :id AND event_type = 'session.revoked'",
        id=account.user_id,
    )
    assert events == [("logout",)]


async def test_sessions_list_and_revocation(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as laptop, api.client() as phone, api.client() as tablet:
        for client in (laptop, phone, tablet):
            await api.sign_in(client, account)
        sessions = (await laptop.get("/api/v1/me/sessions")).json()["items"]
        assert len(sessions) == 3
        assert sum(item["current"] for item in sessions) == 1
        phone_session = next(item["id"] for item in sessions if not item["current"])

        assert (await laptop.delete(f"/api/v1/me/sessions/{phone_session}")).status_code == 204
        statuses = [(await c.get("/api/v1/me")).status_code for c in (laptop, phone, tablet)]
        assert sorted(statuses) == [200, 200, 401]

        revoked = await laptop.delete("/api/v1/me/sessions")
        assert revoked.json() == {"revoked": 1}
        assert (await laptop.get("/api/v1/me")).status_code == 200
        assert [s["current"] for s in (await laptop.get("/api/v1/me/sessions")).json()["items"]] == [True]


async def test_a_user_cannot_revoke_someone_elses_session(api: Api) -> None:
    alice, bob = await api.create_account(), await api.create_account()
    async with api.client() as alice_client, api.client() as bob_client:
        await api.sign_in(alice_client, alice)
        await api.sign_in(bob_client, bob)
        [bob_session] = (await bob_client.get("/api/v1/me/sessions")).json()["items"]
        response = await alice_client.delete(f"/api/v1/me/sessions/{bob_session['id']}")
        assert response.status_code == 404
        assert (await bob_client.get("/api/v1/me")).status_code == 200


async def test_idle_timeout_ends_the_session_and_activity_extends_it(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        for _ in range(3):
            api.clock.advance(timedelta(minutes=14))
            assert (await client.post(REFRESH)).status_code == 200
            assert (await client.get("/api/v1/me")).status_code == 200
        # Background polling does not count as activity.
        api.clock.advance(timedelta(minutes=14))
        assert (await client.post(REFRESH)).status_code == 200
        assert (await client.get("/api/v1/me", headers={"X-Sv-Background": "1"})).status_code == 200
        api.clock.advance(timedelta(minutes=17))
        assert (await client.post(REFRESH)).status_code == 401


async def test_absolute_timeout_ends_even_an_active_session(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        for _ in range(12 * 4 - 1):
            api.clock.advance(timedelta(minutes=15))
            assert (await client.post(REFRESH)).status_code == 200
            assert (await client.get("/api/v1/me")).status_code == 200
        api.clock.advance(timedelta(minutes=15))
        assert (await client.post(REFRESH)).status_code == 401


async def test_an_expired_access_token_is_refused(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        api.clock.advance(timedelta(minutes=15, seconds=1))
        response = await client.get("/api/v1/me")
        assert response.status_code == 401
        assert response.json()["type"] == "/problems/unauthenticated"


async def test_disabling_an_account_ends_its_sessions_at_once(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.execute(
            "UPDATE identity.users SET status = :status WHERE id = :id",
            status=UserStatus.DISABLED.value,
            id=account.user_id,
        )
        assert (await client.get("/api/v1/me")).status_code == 401
        assert (await client.post(REFRESH)).status_code == 401


async def test_garbage_credentials_are_unauthenticated_not_errors(api: Api) -> None:
    async with api.client() as client:
        for value in ("", "x", "%" * 43, "a" * 4096):
            client.cookies.set("__Host-sv_at", value)
            assert (await client.get("/api/v1/me")).status_code == 401
            client.cookies.set("__Secure-sv_rt", value, path="/api/v1/auth/refresh")
            assert (await client.post(REFRESH)).status_code == 401
