"""Two-step sign-in, lockout and throttling (docs/security-architecture.md §3.3-3.4, api-architecture §5).

Runs against real PostgreSQL and Redis. Every failure must answer identically, so no response
reveals whether an account exists, is locked or is disabled.
"""

import asyncio
from datetime import timedelta

import httpx
import pyotp
import pytest
from argon2 import PasswordHasher
from pydantic import SecretStr

from app.modules.identity.models import UserStatus
from tests.api_support import PASSWORD, Account, Api, running_api
from tests.conftest import PostgresServer, RedisServer

LOGIN = "/api/v1/auth/login"
LOGIN_MFA = "/api/v1/auth/login/mfa"


async def password_step(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post(LOGIN, json={"email": email, "password": password})


def assert_generic_failure(response: httpx.Response) -> None:
    assert response.status_code == 401
    body = response.json()
    assert body["type"] == "/problems/invalid-credentials"
    assert "code" not in body
    assert response.cookies.get("__Host-sv_at") is None


async def test_password_alone_never_creates_a_session(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        response = await password_step(client, account.email)
        assert response.status_code == 200
        assert set(response.json()) == {"mfa_token"}
        assert "set-cookie" not in response.headers
        assert (await client.get("/api/v1/me")).status_code == 401


async def test_sign_in_with_totp_creates_a_full_session_in_secure_cookies(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        response = await api.sign_in(client, account)
        assert response.json() == {"session_scope": "full"}
        cookies = response.headers.get_list("set-cookie")
        access = next(c for c in cookies if c.startswith("__Host-sv_at="))
        refresh = next(c for c in cookies if c.startswith("__Secure-sv_rt="))
        for cookie in (access, refresh):
            assert "HttpOnly" in cookie
            assert "Secure" in cookie
        assert "Path=/;" in access
        assert "SameSite=lax" in access
        assert "Path=/api/v1/auth/refresh" in refresh
        assert "SameSite=strict" in refresh
        me = (await client.get("/api/v1/me")).json()
        assert me["user_id"] == str(account.user_id)
        assert me["session_scope"] == "full"
        # Every active account holds its own sessions and login history.
        assert {"auth.session.read.self", "auth.session.revoke.self"} <= set(me["permissions"])


async def test_unknown_email_wrong_password_disabled_and_invited_answer_alike(api: Api) -> None:
    active = await api.create_account()
    disabled = await api.create_account(status=UserStatus.DISABLED)
    invited = await api.create_account(status=UserStatus.INVITED)
    async with api.client() as client:
        responses = [
            await password_step(client, "nobody@dev.example"),
            await password_step(client, active.email, "a wrong but long password"),
            await password_step(client, disabled.email),
            await password_step(client, invited.email),
        ]
    bodies = []
    for response in responses:
        assert_generic_failure(response)
        body = response.json()
        body.pop("request_id")
        bodies.append(body)
    assert all(body == bodies[0] for body in bodies)


async def test_a_wrong_code_counts_as_a_failure_and_the_challenge_is_single_use(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        token = (await password_step(client, account.email)).json()["mfa_token"]
        wrong = await client.post(LOGIN_MFA, json={"mfa_token": token, "code": "000000"})
        assert_generic_failure(wrong)
        [(count,)] = await api.fetch(
            "SELECT failed_login_count FROM identity.users WHERE id = :id", id=account.user_id
        )
        assert count == 1
        right = await client.post(LOGIN_MFA, json={"mfa_token": token, "code": account.code(api.clock.now())})
        assert right.status_code == 200
        again = await client.post(LOGIN_MFA, json={"mfa_token": token, "code": account.code(api.clock.now())})
        assert again.status_code == 401
        assert again.json()["code"] == "login.challenge_expired"
    [(count,)] = await api.fetch(
        "SELECT failed_login_count FROM identity.users WHERE id = :id", id=account.user_id
    )
    assert count == 0


async def test_a_totp_code_cannot_be_replayed(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        code = account.code(api.clock.now())
        token = (await password_step(client, account.email)).json()["mfa_token"]
        replay = await client.post(LOGIN_MFA, json={"mfa_token": token, "code": code})
        assert_generic_failure(replay)


async def test_the_challenge_expires_and_allows_limited_attempts(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        token = (await password_step(client, account.email)).json()["mfa_token"]
        for _ in range(5):
            assert (
                await client.post(LOGIN_MFA, json={"mfa_token": token, "code": "000000"})
            ).status_code == 401
        exhausted = await client.post(
            LOGIN_MFA, json={"mfa_token": token, "code": account.code(api.clock.now())}
        )
        assert exhausted.json()["code"] == "login.challenge_expired"

        token = (await password_step(client, account.email)).json()["mfa_token"]
        api.clock.advance(timedelta(minutes=5, seconds=1))
        expired = await client.post(
            LOGIN_MFA, json={"mfa_token": token, "code": account.code(api.clock.now())}
        )
        assert expired.json()["code"] == "login.challenge_expired"


async def test_mfa_request_needs_exactly_one_kind_of_code(api: Api) -> None:
    async with api.client() as client:
        for body in ({"mfa_token": "x"}, {"mfa_token": "x", "code": "1", "recovery_code": "2"}):
            response = await client.post(LOGIN_MFA, json=body)
            assert response.status_code == 422
            assert response.json()["type"] == "/problems/validation-error"


async def test_ten_failures_lock_the_account_without_revealing_it(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        for _ in range(10):
            assert_generic_failure(await password_step(client, account.email, "a wrong but long password"))
        # The right password is now refused with the same answer as a wrong one.
        assert_generic_failure(await password_step(client, account.email))
    [(locked_until,)] = await api.fetch(
        "SELECT locked_until FROM identity.users WHERE id = :id", id=account.user_id
    )
    assert locked_until > api.clock.now()
    events = await api.fetch(
        "SELECT event_type FROM audit.security_events WHERE user_id = :id AND event_type = 'account.locked'",
        id=account.user_id,
    )
    assert len(events) == 1
    # From the sixth failure on, attempts are slowed: 1, 2, 4, 8 ... seconds.
    assert api.sleep.calls
    assert max(api.sleep.calls) <= 30

    api.clock.advance(timedelta(minutes=16))
    async with api.client() as client:
        assert (await password_step(client, account.email)).status_code == 200


async def test_failures_outside_the_window_start_a_new_count(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        for _ in range(9):
            await password_step(client, account.email, "a wrong but long password")
        api.clock.advance(timedelta(minutes=16))
        await password_step(client, account.email, "a wrong but long password")
        assert (await password_step(client, account.email)).status_code == 200
    [(count,)] = await api.fetch(
        "SELECT failed_login_count FROM identity.users WHERE id = :id", id=account.user_id
    )
    assert count == 1


async def test_concurrent_failures_are_all_counted(api: Api) -> None:
    """The counter is one atomic UPDATE, so simultaneous failures cannot overwrite each other."""
    account = await api.create_account()
    async with api.client() as client:
        await asyncio.gather(
            *(password_step(client, account.email, "a wrong but long password") for _ in range(6))
        )
    [(count,)] = await api.fetch(
        "SELECT failed_login_count FROM identity.users WHERE id = :id", id=account.user_id
    )
    assert count == 6


async def test_twenty_failures_from_one_address_block_it(api: Api) -> None:
    account = await api.create_account()
    async with api.client(ip="198.51.100.20") as client:
        for _ in range(20):
            assert (await password_step(client, "nobody@dev.example")).status_code == 401
        blocked = await password_step(client, account.email)
        assert blocked.status_code == 429
        assert blocked.json()["type"] == "/problems/rate-limited"
        assert int(blocked.headers["Retry-After"]) > 0
    # Another address is not affected.
    async with api.client() as client:
        assert (await password_step(client, account.email)).status_code == 200
    events = await api.fetch(
        "SELECT details->>'rule' FROM audit.security_events WHERE event_type = 'ratelimit.tripped' "
        "AND ip = '198.51.100.20'"
    )
    assert events == [("login.ip_block",)]


async def test_sign_in_fails_closed_when_the_rate_limit_store_is_down(
    migrated_postgres: PostgresServer, redis: RedisServer
) -> None:
    unreachable = SecretStr("redis://hrms_ratelimit:x@127.0.0.1:1/0")
    async with running_api(migrated_postgres, redis, redis_url=unreachable) as api:
        account = await api.create_account()
        async with api.client() as client:
            response = await password_step(client, account.email)
            assert response.status_code == 503
            assert response.json()["type"] == "/problems/temporarily-unavailable"
            assert "Retry-After" in response.headers


async def test_recovery_code_gives_an_enrolment_only_session(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.step_up(client, account)
        codes = (await client.post("/api/v1/me/mfa/recovery-codes")).json()["codes"]
        assert len(codes) == 10
        assert len(set(codes)) == 10

    async with api.client() as client:
        token = (await password_step(client, account.email)).json()["mfa_token"]
        signed_in = await client.post(LOGIN_MFA, json={"mfa_token": token, "recovery_code": codes[0].lower()})
        assert signed_in.json() == {"session_scope": "mfa_enrolment"}
        # Only the enrolment endpoints work; everything else says why.
        assert (await client.get("/api/v1/me")).json()["permissions"] == []
        for method, path in (("GET", "/api/v1/me/sessions"), ("POST", "/api/v1/auth/step-up")):
            refused = await client.request(
                method, path, json={"code": "000000"} if method == "POST" else None
            )
            assert refused.status_code == 403
            assert refused.json()["type"] == "/problems/mfa-enrolment-required"
        # Enrolling a new authenticator completes the recovery: the session becomes full.
        setup = (await client.post("/api/v1/me/mfa/totp/setup", json={"label": "New phone"})).json()
        code = totp_code(setup["secret"], api)
        confirmed = await client.post(
            "/api/v1/me/mfa/totp/confirm", json={"factor_id": setup["factor_id"], "code": code}
        )
        assert confirmed.json() == {"session_scope": "full"}
        assert (await client.get("/api/v1/me/sessions")).status_code == 200

    # A recovery code works once.
    async with api.client() as client:
        token = (await password_step(client, account.email)).json()["mfa_token"]
        reused = await client.post(LOGIN_MFA, json={"mfa_token": token, "recovery_code": codes[0]})
        assert_generic_failure(reused)
    events = await api.fetch(
        "SELECT severity FROM audit.security_events "
        "WHERE user_id = :id AND event_type = 'mfa.recovery_code_used'",
        id=account.user_id,
    )
    assert events == [("high",)]


def totp_code(secret: str, api: Api) -> str:
    return pyotp.TOTP(secret).at(api.clock.now())


@pytest.mark.parametrize("header", [{}, {"X-Requested-With": "XMLHttpRequest"}])
async def test_unsafe_requests_need_the_csrf_header(api: Api, header: dict[str, str]) -> None:
    async with api.client(csrf=False) as client:
        response = await client.post(
            LOGIN, json={"email": "nobody@dev.example", "password": PASSWORD}, headers=header
        )
        assert response.status_code == 403
        assert response.json()["code"] == "csrf"
        assert response.headers["X-Request-ID"]


async def test_unsafe_requests_from_another_origin_are_refused(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        response = await client.post(
            LOGIN,
            json={"email": account.email, "password": PASSWORD},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        same_origin = await client.post(
            LOGIN,
            json={"email": account.email, "password": PASSWORD},
            headers={"Origin": "https://hrms.test"},
        )
        assert same_origin.status_code == 200


async def test_rehash_on_sign_in_when_parameters_change(api: Api, account_with_old_hash: Account) -> None:
    async with api.client() as client:
        await api.sign_in(client, account_with_old_hash)
    [(password_hash,)] = await api.fetch(
        "SELECT password_hash FROM identity.credentials WHERE user_id = :id", id=account_with_old_hash.user_id
    )
    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=1$")


@pytest.fixture
async def account_with_old_hash(api: Api) -> Account:
    account = await api.create_account()
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    await api.execute(
        "UPDATE identity.credentials SET password_hash = :h WHERE user_id = :id", h=weak, id=account.user_id
    )
    return account
