"""Administrator MFA reset and re-enrolment (docs/security-architecture.md §3.3, threat model T17).

The reset needs `security.mfa.reset`, step-up and a reason, never targets one's own account,
and ends every factor, recovery code, session and trusted device. Re-enrolment needs the
emailed link *and* the password; it opens only an enrolment session, and confirming a new
authenticator there returns new recovery codes and a full session.
"""

import uuid
from datetime import timedelta

import httpx
import pyotp
import pytest

from app.modules.identity.invariants import MfaInvariantError, require_mfa
from app.modules.identity.models import AuthSession, User, UserStatus
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from app.platform.security import totp
from tests.api_support import PASSWORD, Account, Api, link_token

REASON = "Lost phone; identity checked by video call with the HR team"


async def system_admin(api: Api, client: httpx.AsyncClient) -> Account:
    admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    await api.sign_in(client, admin)
    await api.step_up(client, admin)
    return admin


async def reset(client: httpx.AsyncClient, target: Account) -> httpx.Response:
    return await client.post(f"/api/v1/users/{target.user_id}/mfa-reset", json={"reason": REASON})


async def listed_user(client: httpx.AsyncClient, account: Account) -> dict[str, object]:
    """The account's row in `GET /users`, paging through: the API tests share one database."""
    cursor: str | None = None
    while True:
        params = {"limit": 100} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/api/v1/users", params=params)).json()
        for user in page["items"]:
            if user["id"] == str(account.user_id):
                return dict(user)
        cursor = page["next_cursor"]
        assert cursor is not None, "the account is not in the user list"


async def reenrolment_link(api: Api, account: Account) -> str:
    [email] = [m for m in await api.deliver() if m.to == account.email and "/mfa-reenrolment/" in m.body]
    return link_token(email)


async def start(client: httpx.AsyncClient, token: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post(f"/api/v1/auth/mfa-reenrolment/{token}", json={"password": password})


async def enrol(api: Api, client: httpx.AsyncClient) -> tuple[str, httpx.Response]:
    """Set up and confirm a new authenticator in the current session; returns its secret."""
    setup = await client.post("/api/v1/me/mfa/totp/setup", json={})
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    api.clock.advance(timedelta(seconds=totp.STEP_SECONDS))
    confirmed = await client.post(
        "/api/v1/me/mfa/totp/confirm",
        json={"factor_id": setup.json()["factor_id"], "code": pyotp.TOTP(secret).at(api.clock.now())},
    )
    return secret, confirmed


async def test_reset_and_re_enrolment_end_to_end(api: Api) -> None:  # noqa: PLR0915
    target = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as admin_client, api.client() as target_client:
        admin = await system_admin(api, admin_client)
        await api.sign_in(target_client, target)
        assert (await target_client.get("/api/v1/users")).status_code == 200

        assert (await reset(admin_client, target)).status_code == 202
        # Every session ended at once, and no refresh brings it back.
        assert (await target_client.get("/api/v1/me")).status_code == 401
        assert (await target_client.post("/api/v1/auth/refresh")).status_code == 401

        assert (await listed_user(admin_client, target))["mfa_reenrolment_required"] is True

    factors = await api.count("mfa_factors", {"user_id": target.user_id, "revoked_at": None})
    codes = await api.count(
        "recovery_codes", {"user_id": target.user_id, "revoked_at": None, "used_at": None}
    )
    [user] = await api.records(User, {"id": target.user_id})
    assert (factors, codes, user.mfa_reenrolment_required) == (0, 0, True)
    [audited] = await api.records(
        AuditLogRecord, {"actor_user_id": admin.user_id, "target_id": str(target.user_id)}
    )
    action, permission, reason, changes = (
        audited.action,
        audited.permission_used,
        audited.reason,
        audited.changes or {},
    )
    assert (action, permission, reason) == ("identity.mfa.reset", "security.mfa.reset", REASON)
    assert changes == {"mfa_reenrolment_required": {"old": False, "new": True}}
    [event] = await api.find("security_events", {"user_id": target.user_id, "event_type": "mfa.reset"})
    severity, details = event["severity"], event["details"]
    assert severity == "high"
    assert details["by_user_id"] == str(admin.user_id)
    assert details["sessions_revoked"] == 1

    token = await reenrolment_link(api, target)
    async with api.client() as client:
        # A password alone signs in nowhere: the normal sign-in is refused like any other.
        refused = await client.post("/api/v1/auth/login", json={"email": target.email, "password": PASSWORD})
        assert refused.status_code == 401
        assert refused.json()["type"] == "/problems/invalid-credentials"

        wrong = await start(client, token, "not the right password at all")
        assert wrong.status_code == 422
        assert wrong.json()["errors"][0]["code"] == "password.incorrect"

        started = await start(client, token)
        assert started.status_code == 200, started.text
        assert started.json() == {"session_scope": "mfa_enrolment"}
        # The enrolment session reaches nothing else, whatever roles the account holds.
        denied = await client.get("/api/v1/users")
        assert denied.status_code == 403
        assert denied.json()["type"] == "/problems/mfa-enrolment-required"

        secret, confirmed = await enrol(api, client)
        assert confirmed.status_code == 200, confirmed.text
        body = confirmed.json()
        assert body["session_scope"] == "full"
        assert len(body["recovery_codes"]) == 10
        assert (await client.get("/api/v1/users")).status_code == 200

    # The link worked once.
    async with api.client() as client:
        used = await start(client, token)
        assert used.status_code == 409
        assert used.json()["code"] == "mfa_reenrolment.invalid"
    [user] = await api.records(User, {"id": target.user_id})
    assert user.mfa_reenrolment_required is False
    # The new authenticator signs in as usual.
    async with api.client() as client:
        target.totp_secret = secret
        await api.sign_in(client, target)
    events = [
        event["event_type"]
        for event in await api.find(
            "security_events",
            {"user_id": target.user_id, "event_type": {"$regex": "^mfa[.]reenrolment"}},
            sort=[("recorded_at", 1), ("_id", 1)],
        )
    ]
    assert events == ["mfa.reenrolment_started", "mfa.reenrolment_completed"]


async def test_reset_needs_step_up_a_reason_and_never_targets_oneself(api: Api) -> None:
    target = await api.create_account()
    admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, admin)
        needs = await reset(client, target)
        assert needs.status_code == 403
        assert needs.json()["type"] == "/problems/step-up-required"
        await api.step_up(client, admin)
        no_reason = await client.post(f"/api/v1/users/{target.user_id}/mfa-reset", json={"reason": " "})
        assert no_reason.status_code == 422
        own = await reset(client, admin)
        assert own.status_code == 403
        assert own.json()["code"] == "sod.own_account"
        missing = await client.post(f"/api/v1/users/{uuid.uuid4()}/mfa-reset", json={"reason": REASON})
        assert missing.status_code == 404


@pytest.mark.parametrize("status", [UserStatus.INVITED, UserStatus.DISABLED])
async def test_only_an_active_account_is_reset(api: Api, status: UserStatus) -> None:
    target = await api.create_account(status=status)
    async with api.client() as client:
        await system_admin(api, client)
        refused = await reset(client, target)
        assert refused.status_code == 409
        assert refused.json()["code"] == "user.not_active"


async def test_a_locked_account_cannot_check_its_password_through_re_enrolment(api: Api) -> None:
    """Lockout guards every password check: a stolen link cannot be used to guess the password."""
    target = await api.create_account()
    async with api.client() as client:
        await system_admin(api, client)
        await reset(client, target)
    token = await reenrolment_link(api, target)
    async with api.client() as client:
        for _ in range(10):
            assert (await start(client, token, "a wrong guess of the password")).status_code == 422
        locked = await start(client, token)
        assert locked.status_code == 422
        assert locked.json()["errors"][0]["code"] == "account.locked"
    # The link was not used up by the refused attempts.
    await api.update("users", {"_id": target.user_id}, {"locked_until": None})
    async with api.client() as client:
        assert (await start(client, token)).status_code == 200


async def test_a_new_reset_replaces_the_earlier_link_and_bad_links_fail_cheaply(api: Api) -> None:
    target = await api.create_account()
    async with api.client() as client:
        await system_admin(api, client)
        await reset(client, target)
        first = await reenrolment_link(api, target)
        await reset(client, target)
        second = await reenrolment_link(api, target)
    async with api.client() as client:
        for token in (first, "x" * 43, "short"):
            refused = await start(client, token)
            assert refused.status_code == 409
            assert refused.json()["code"] == "mfa_reenrolment.invalid"
        assert (await start(client, second)).status_code == 200


async def test_the_link_expires(api: Api) -> None:
    target = await api.create_account()
    async with api.client() as client:
        await system_admin(api, client)
        await reset(client, target)
    token = await reenrolment_link(api, target)
    api.clock.advance(timedelta(hours=24, seconds=1))
    async with api.client() as client:
        assert (await start(client, token)).json()["code"] == "mfa_reenrolment.invalid"


async def test_the_mfa_invariants_hold_during_re_enrolment(api: Api) -> None:
    """The MFA backstop (identity/invariants.py): every change that could break the rules
    registers the check, which runs before the commit; a violation stores nothing."""
    target = await api.create_account()
    async with api.client() as client:
        await system_admin(api, client)
        await reset(client, target)
    # Clearing the flag without a confirmed factor is refused.
    with pytest.raises(MfaInvariantError, match="needs a confirmed MFA factor"):  # noqa: PT012
        async with api.database.unit_of_work() as session:
            user = await session.get(User, target.user_id)
            assert user is not None
            user.mfa_reenrolment_required = False
            require_mfa(session, target.user_id)
    # No full session can be opened for an account waiting to re-enrol, or without a factor.
    now = api.clock.now()
    with pytest.raises(MfaInvariantError, match="a full session needs a confirmed MFA factor"):  # noqa: PT012
        async with api.database.unit_of_work() as session:
            session.add(
                AuthSession(
                    user_id=target.user_id,
                    client_type="web",
                    scope="full",
                    last_activity_at=now,
                    idle_expires_at=now + timedelta(hours=1),
                    absolute_expires_at=now + timedelta(hours=2),
                )
            )
            require_mfa(session, target.user_id)
    # An account with an open full session cannot be marked as waiting to re-enrol.
    other = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, other)
    with pytest.raises(MfaInvariantError, match="a full session needs a confirmed MFA factor"):  # noqa: PT012
        async with api.database.unit_of_work() as session:
            user = await session.get(User, other.user_id)
            assert user is not None
            user.mfa_reenrolment_required = True
            require_mfa(session, other.user_id)
    [stored] = await api.records(User, {"id": other.user_id})
    assert stored.mfa_reenrolment_required is False
