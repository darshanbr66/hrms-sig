"""Secrets never leave through logs, audit records, security events or responses
(CLAUDE.md rules 6 and 8, docs/security-architecture.md §2 `secret`, §7).

A full account lifecycle runs (invite activation, sign-in, step-up, new factor, recovery
codes, password change, refresh, sign-out) while every log record is captured. Then every
secret the flow handled is searched for in the logs, in every audit and security row the
flow wrote, and in the responses that must not carry it.
"""

import json
import logging
from datetime import timedelta

import pyotp
import pytest

from app.modules.identity import public as identity
from app.platform.logging import JsonFormatter
from tests.api_support import PASSWORD, Api, refresh_cookie

NEW_PASSWORD = "another quite long passphrase 77"


async def test_no_secret_reaches_logs_audit_or_events(api: Api, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secrets_seen: set[str] = {PASSWORD, NEW_PASSWORD}

    async with api.database.unit_of_work() as session:
        email = f"leak-check-{api.clock.now().timestamp()}@dev.example"
        user_id = await identity.create_invited_account(session, email)
        invite = await identity.issue_invite(session, user_id, api.clock.now())
    secrets_seen.add(invite)

    async with api.client() as client:
        base = f"/api/v1/auth/invite/{invite}"
        enrolment = (await client.post(f"{base}/password", json={"password": PASSWORD})).json()[
            "enrolment_token"
        ]
        setup = (await client.post(f"{base}/mfa/totp/setup", json={"enrolment_token": enrolment})).json()
        secrets_seen |= {enrolment, setup["secret"]}
        totp = pyotp.TOTP(setup["secret"])
        activated = await client.post(
            f"{base}/mfa/totp/confirm",
            json={
                "enrolment_token": enrolment,
                "factor_id": setup["factor_id"],
                "code": totp.at(api.clock.now()),
            },
        )
        secrets_seen |= set(activated.json()["recovery_codes"])
        secrets_seen |= {client.cookies.get("__Host-sv_at") or "", refresh_cookie(client)}

        api.clock.advance(timedelta(seconds=30))
        login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        mfa_token = login.json()["mfa_token"]
        secrets_seen.add(mfa_token)
        await client.post(
            "/api/v1/auth/login/mfa", json={"mfa_token": mfa_token, "code": totp.at(api.clock.now())}
        )
        await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "a wrong password entirely"}
        )
        secrets_seen.add("a wrong password entirely")

        api.clock.advance(timedelta(seconds=30))
        await client.post("/api/v1/auth/step-up", json={"code": totp.at(api.clock.now())})
        codes = (await client.post("/api/v1/me/mfa/recovery-codes")).json()["codes"]
        secrets_seen |= set(codes)
        second = (await client.post("/api/v1/me/mfa/totp/setup", json={"label": "Spare"})).json()
        secrets_seen.add(second["secret"])
        await client.post(
            "/api/v1/me/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
        )
        await client.post("/api/v1/auth/refresh")
        secrets_seen |= {client.cookies.get("__Host-sv_at") or "", refresh_cookie(client)}
        await client.post("/api/v1/auth/logout")

    secrets_seen.discard("")
    formatter = JsonFormatter()
    # The test's own HTTP client logs the URLs it requests; only the application's records count.
    server_records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logs = "\n".join(formatter.format(record) for record in server_records)
    assert logs, "the flow should have logged requests"
    rows = await api.fetch(
        "SELECT to_jsonb(a)::text FROM audit.audit_log a WHERE target_id = :id OR actor_user_id = :uid "
        "UNION ALL SELECT to_jsonb(e)::text FROM audit.security_events e WHERE user_id = :uid",
        id=str(user_id),
        uid=user_id,
    )
    stored = "\n".join(row[0] for row in rows)
    assert "login.succeeded" in stored
    for secret in secrets_seen:
        assert secret not in logs, "a secret was logged"
        assert secret not in stored, "a secret was written to an audit or security record"
        compact = secret.replace("-", "")
        assert compact not in stored


async def test_responses_never_carry_hashes_or_stored_secrets(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.step_up(client, account)
        bodies = [
            (await client.get(path)).text
            for path in (
                "/api/v1/me",
                "/api/v1/me/mfa/factors",
                "/api/v1/me/sessions",
                "/api/v1/me/login-history",
            )
        ]
    for body in bodies:
        assert "$argon2" not in body
        assert account.totp_secret not in body
        assert "password" not in json.loads(body).__repr__().lower()


async def test_an_unhandled_error_reveals_nothing(api: Api) -> None:
    async with api.client() as client:
        client.cookies.set("__Host-sv_at", "A" * 43)
        response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert "Traceback" not in response.text
    assert "sql" not in response.text.lower()
