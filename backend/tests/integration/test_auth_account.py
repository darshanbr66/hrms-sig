"""Step-up, password change, MFA factors and recovery codes, invite activation (security-architecture §3)."""

import asyncio
from datetime import timedelta

import httpx
import pyotp
import pytest

from app.modules.identity import public as identity
from tests.api_support import PASSWORD, Account, Api, refresh_cookie

STEP_UP = "/api/v1/auth/step-up"


def problem_type(response: httpx.Response) -> str:
    return str(response.json()["type"])


# --- step-up ------------------------------------------------------------------------------


async def test_step_up_needs_a_current_totp_code_and_lasts_ten_minutes(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        refused = await client.post("/api/v1/me/mfa/recovery-codes")
        assert refused.status_code == 403
        assert problem_type(refused) == "/problems/step-up-required"
        assert refused.json()["max_age_seconds"] == 600

        before = client.cookies.get("__Host-sv_at")
        stepped = await api.step_up(client, account)
        assert "step_up_expires_at" in stepped.json()
        assert client.cookies.get("__Host-sv_at") != before  # tokens rotate on step-up
        assert (await client.post("/api/v1/me/mfa/recovery-codes")).status_code == 200

        api.clock.advance(timedelta(minutes=10, seconds=1))
        await client.post("/api/v1/auth/refresh")
        assert (
            problem_type(await client.post("/api/v1/me/mfa/recovery-codes")) == "/problems/step-up-required"
        )


@pytest.mark.parametrize(
    "payload", [{"code": PASSWORD}, {"code": "0000-0000-0000-0000"}, {"password": PASSWORD}]
)
async def test_a_password_or_recovery_code_never_satisfies_step_up(api: Api, payload: dict[str, str]) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        response = await client.post(STEP_UP, json=payload)
        assert response.status_code == 422
        [(step_up_at,)] = await api.fetch(
            "SELECT step_up_at FROM identity.sessions WHERE user_id = :id", id=account.user_id
        )
        assert step_up_at is None


async def test_step_up_attempts_are_limited_per_session(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        for _ in range(5):
            assert (await client.post(STEP_UP, json={"code": "000000"})).status_code == 422
        assert (await client.post(STEP_UP, json={"code": account.code(api.clock.now())})).status_code == 429


async def test_the_old_refresh_token_is_retired_by_step_up(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        old_refresh = refresh_cookie(client)
        await api.step_up(client, account)
        async with api.client() as thief:
            thief.cookies.set("__Secure-sv_rt", old_refresh, path="/api/v1/auth/refresh")
            assert (await thief.post("/api/v1/auth/refresh")).status_code == 401


# --- password -----------------------------------------------------------------------------


async def test_changing_the_password(api: Api) -> None:
    account = await api.create_account()
    new_password = "a different passphrase 2030"
    async with api.client() as current, api.client() as other:
        await api.sign_in(current, account)
        await api.sign_in(other, account)
        body = {"current_password": PASSWORD, "new_password": new_password}
        assert (
            problem_type(await current.post("/api/v1/me/password", json=body)) == "/problems/step-up-required"
        )
        await api.step_up(current, account)

        wrong = await current.post("/api/v1/me/password", json=body | {"current_password": "not it at all"})
        assert wrong.status_code == 422
        assert wrong.json()["errors"][0]["code"] == "password.incorrect"
        weak = await current.post("/api/v1/me/password", json=body | {"new_password": "passwordpassword"})
        assert weak.json()["errors"][0] == {
            "field": "new_password",
            "code": "password.breached",
            "message": weak.json()["errors"][0]["message"],
        }
        short = await current.post("/api/v1/me/password", json=body | {"new_password": "short"})
        assert short.json()["errors"][0]["code"] == "password.too_short"

        assert (await current.post("/api/v1/me/password", json=body)).status_code == 204
        # Every other session ends; this one stays.
        assert (await current.get("/api/v1/me")).status_code == 200
        assert (await other.get("/api/v1/me")).status_code == 401

    async with api.client() as client:
        old = await client.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        assert old.status_code == 401
        new = await client.post("/api/v1/auth/login", json={"email": account.email, "password": new_password})
        assert new.status_code == 200


async def test_a_locked_account_cannot_check_its_password_through_a_password_change(api: Api) -> None:
    """Wrong current passwords count towards the lockout, and while the account is locked the
    current password is not checked at all: a held session is no way around the lockout."""
    account = await api.create_account()
    new_password = "a different passphrase 2030"
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.step_up(client, account)
        wrong = {"current_password": "not it at all", "new_password": new_password}
        for _ in range(10):  # the default lockout threshold
            response = await client.post("/api/v1/me/password", json=wrong)
            assert response.json()["errors"][0]["code"] == "password.incorrect"
        [(locked_until,)] = await api.fetch(
            "SELECT locked_until FROM identity.users WHERE id = :id", id=account.user_id
        )
        assert locked_until is not None

        right = {"current_password": PASSWORD, "new_password": new_password}
        locked = await client.post("/api/v1/me/password", json=right)
        assert locked.status_code == 422
        assert locked.json()["errors"][0]["field"] == "current_password"
        assert locked.json()["errors"][0]["code"] == "account.locked"
        assert "15 minutes" in locked.json()["errors"][0]["message"]

        # The lock ends on time, and the password change then works.
        api.clock.advance(timedelta(minutes=15, seconds=1))
        await client.post("/api/v1/auth/refresh")
        await api.step_up(client, account)
        assert (await client.post("/api/v1/me/password", json=right)).status_code == 204

    async with api.client() as client:
        login = await client.post(
            "/api/v1/auth/login", json={"email": account.email, "password": new_password}
        )
        assert login.status_code == 200


# --- MFA factors and recovery codes ---------------------------------------------------------


async def add_factor(api: Api, client: httpx.AsyncClient, label: str = "Second phone") -> tuple[str, str]:
    setup = await client.post("/api/v1/me/mfa/totp/setup", json={"label": label})
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert body["qr_code"].startswith("data:image/svg+xml;base64,")
    confirm = await client.post(
        "/api/v1/me/mfa/totp/confirm",
        json={"factor_id": body["factor_id"], "code": pyotp.TOTP(body["secret"]).at(api.clock.now())},
    )
    assert confirm.status_code == 200, confirm.text
    return body["factor_id"], body["secret"]


async def test_adding_and_removing_factors_never_leaves_none(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        assert (
            problem_type(await client.post("/api/v1/me/mfa/totp/setup", json={}))
            == "/problems/step-up-required"
        )
        await api.step_up(client, account)
        new_factor, new_secret = await add_factor(api, client)

        factors = (await client.get("/api/v1/me/mfa/factors")).json()["items"]
        assert len(factors) == 2
        assert all("secret" not in factor for factor in factors)
        original = next(f["id"] for f in factors if f["id"] != new_factor)

        assert (await client.delete(f"/api/v1/me/mfa/factors/{original}")).status_code == 204
        last = await client.delete(f"/api/v1/me/mfa/factors/{new_factor}")
        assert last.status_code == 409
        assert last.json()["code"] == "mfa.last_factor"

    # The remaining factor is the one that signs in now.
    replacement = Account(account.user_id, account.email, new_secret)
    async with api.client() as client:
        await api.sign_in(client, replacement)


async def test_a_wrong_confirmation_code_leaves_the_factor_unconfirmed(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.step_up(client, account)
        setup = (await client.post("/api/v1/me/mfa/totp/setup", json={})).json()
        wrong = await client.post(
            "/api/v1/me/mfa/totp/confirm", json={"factor_id": setup["factor_id"], "code": "000000"}
        )
        assert wrong.status_code == 422
        assert len((await client.get("/api/v1/me/mfa/factors")).json()["items"]) == 1
        api.clock.advance(timedelta(minutes=16))
        await client.post("/api/v1/auth/refresh")
        await api.step_up(client, account)
        late = await client.post(
            "/api/v1/me/mfa/totp/confirm",
            json={"factor_id": setup["factor_id"], "code": pyotp.TOTP(setup["secret"]).at(api.clock.now())},
        )
        assert late.json()["code"] == "mfa.setup_expired"


async def test_concurrent_removals_cannot_remove_every_factor(api: Api) -> None:
    """Two requests removing the two remaining factors at once: one must lose (rule 15)."""
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.step_up(client, account)
        await add_factor(api, client)
        ids = [f["id"] for f in (await client.get("/api/v1/me/mfa/factors")).json()["items"]]
        results = await asyncio.gather(*(client.delete(f"/api/v1/me/mfa/factors/{factor}") for factor in ids))
        assert sorted(r.status_code for r in results) == [204, 409]
    rows = await api.fetch(
        "SELECT count(*) FROM identity.mfa_factors "
        "WHERE user_id = :id AND confirmed_at IS NOT NULL AND revoked_at IS NULL",
        id=account.user_id,
    )
    assert rows == [(1,)]


async def test_the_database_refuses_an_active_account_without_a_factor(api: Api) -> None:
    account = await api.create_account()
    with pytest.raises(Exception, match="must keep a confirmed MFA factor"):
        await api.execute(
            "UPDATE identity.mfa_factors SET revoked_at = now() WHERE user_id = :id", id=account.user_id
        )


async def test_secrets_are_encrypted_at_rest(api: Api) -> None:
    account = await api.create_account()
    [(ciphertext,)] = await api.fetch(
        "SELECT secret_ciphertext FROM identity.mfa_factors WHERE user_id = :id", id=account.user_id
    )
    assert account.totp_secret.encode() not in bytes(ciphertext)


async def test_regenerating_recovery_codes_replaces_the_old_ones(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        await api.step_up(client, account)
        first = (await client.post("/api/v1/me/mfa/recovery-codes")).json()["codes"]
        second = (await client.post("/api/v1/me/mfa/recovery-codes")).json()["codes"]
        assert not set(first) & set(second)
    async with api.client() as client:
        token = (
            await client.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        ).json()
        old = await client.post("/api/v1/auth/login/mfa", json={**token, "recovery_code": first[0]})
        assert old.status_code == 401
        new = await client.post("/api/v1/auth/login/mfa", json={**token, "recovery_code": second[0]})
        assert new.json() == {"session_scope": "mfa_enrolment"}
    stored = await api.fetch(
        "SELECT code_hash FROM identity.recovery_codes WHERE user_id = :id", id=account.user_id
    )
    assert all(code.replace("-", "").encode() not in bytes(row[0]) for row in stored for code in second)


# --- invite activation --------------------------------------------------------------------


async def invited_account(api: Api) -> tuple[str, str]:
    """An invited account and its invite token, created the way the bootstrap creates them."""
    async with api.database.unit_of_work() as session:
        email = f"invited-{api.clock.now().timestamp()}@dev.example"
        user_id = await identity.create_invited_account(session, email)
        token = await identity.issue_invite(session, user_id, api.clock.now())
    return email, token


async def test_invite_activation_end_to_end(api: Api) -> None:
    email, token = await invited_account(api)
    base = f"/api/v1/auth/invite/{token}"
    async with api.client() as client:
        assert (await client.get(base)).json() == {"valid": True, "first_name": None}
        weak = await client.post(f"{base}/password", json={"password": "passwordpassword"})
        assert weak.json()["errors"][0]["code"] == "password.breached"
        enrolment = (await client.post(f"{base}/password", json={"password": PASSWORD})).json()[
            "enrolment_token"
        ]

        # The invite link alone cannot enrol an authenticator: steps 2 and 3 need the
        # enrolment token from the browser that set the password.
        assert (
            await client.post(f"{base}/mfa/totp/setup", json={"enrolment_token": token})
        ).status_code == 409
        setup = (await client.post(f"{base}/mfa/totp/setup", json={"enrolment_token": enrolment})).json()
        code = pyotp.TOTP(setup["secret"]).at(api.clock.now())
        activated = await client.post(
            f"{base}/mfa/totp/confirm",
            json={"enrolment_token": enrolment, "factor_id": setup["factor_id"], "code": code},
        )
        assert activated.status_code == 200, activated.text
        assert len(activated.json()["recovery_codes"]) == 10
        me = (await client.get("/api/v1/me")).json()
        assert me["email"] == email
        assert me["session_scope"] == "full"

        # Single use: the link no longer works.
        assert (await client.get(base)).json() == {"valid": False, "first_name": None}
        assert (await client.post(f"{base}/password", json={"password": PASSWORD})).json()[
            "code"
        ] == "invite.invalid"

    [(status, verified)] = await api.fetch(
        "SELECT status, email_verified_at FROM identity.users WHERE email = :e", e=email
    )
    assert status == "active"
    assert verified is not None


async def test_an_invite_expires_after_72_hours(api: Api) -> None:
    _, token = await invited_account(api)
    api.clock.advance(timedelta(hours=72, seconds=1))
    async with api.client() as client:
        assert (await client.get(f"/api/v1/auth/invite/{token}")).json()["valid"] is False


async def test_an_unconfirmed_invite_cannot_sign_in(api: Api) -> None:
    email, token = await invited_account(api)
    async with api.client() as client:
        await client.post(f"/api/v1/auth/invite/{token}/password", json={"password": PASSWORD})
        response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert response.status_code == 401
