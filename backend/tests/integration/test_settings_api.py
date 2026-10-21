"""The settings registry API: authority, step-up, validation, audit, and settings taking effect."""

from collections.abc import AsyncIterator

import httpx
import pytest

from app.modules.identity.models import AuthSession, User
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api

THRESHOLD = "security.lockout.threshold"


async def signed_in_admin(
    api: Api, client: httpx.AsyncClient, role: RoleKey = RoleKey.SYSTEM_ADMIN
) -> Account:
    account = await api.create_account(roles=(role.value,))
    await api.sign_in(client, account)
    return account


@pytest.fixture(autouse=True)
async def restore_defaults(api: Api) -> AsyncIterator[None]:
    """Settings belong to an organization and every test has one of its own, so each starts at the
    defaults; any overrides left are removed afterwards."""
    yield
    await api.worker_database.collection("organization_settings").delete_many(
        {"organization_id": api.organization_id}
    )


async def test_settings_list_the_registry_with_defaults(api: Api) -> None:
    async with api.client() as client:
        await signed_in_admin(api, client, RoleKey.HR_ADMIN)
        items = {item["key"]: item for item in (await client.get("/api/v1/settings")).json()["items"]}
    assert items[THRESHOLD] | {"description": ""} == {
        "key": THRESHOLD,
        "description": "",
        "value": 10,
        "default": 10,
        "minimum": 3,
        "maximum": 20,
        "unit": "attempts",
        "overridden": False,
        "permission": "security.settings.manage",
    }
    assert items["email.delivery.max_attempts"]["permission"] == "settings.manage"


async def test_reading_is_not_changing(api: Api) -> None:
    async with api.client() as client:
        await signed_in_admin(api, client, RoleKey.HR_ADMIN)
        refused = await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": 5})
        assert refused.status_code == 403


async def test_a_super_admin_cannot_change_security_settings(api: Api) -> None:
    async with api.client() as client:
        await signed_in_admin(api, client, RoleKey.SUPER_ADMIN)
        assert (await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": 5})).status_code == 403


async def test_changes_need_step_up_and_are_audited(api: Api) -> None:
    async with api.client() as client:
        admin = await signed_in_admin(api, client)
        missing = await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": 6})
        assert missing.json()["type"] == "/problems/step-up-required"
        await api.step_up(client, admin)
        changed = await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": 6})
        assert changed.json()["value"] == 6
        assert changed.json()["overridden"] is True
        restored = await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": None})
        assert restored.json() | {"description": ""} == changed.json() | {
            "value": 10,
            "overridden": False,
            "description": "",
        }
    rows = [
        (row.permission_used, (row.changes or {})["value"])
        for row in await api.records(
            AuditLogRecord,
            {"actor_user_id": admin.user_id, "action": "settings.value.changed"},
            sort=[("recorded_at", 1), ("id", 1)],
        )
    ]
    assert rows == [
        ("security.settings.manage", {"old": 10, "new": 6}),
        ("security.settings.manage", {"old": 6, "new": 10}),
    ]


@pytest.mark.parametrize("value", [2, 21, 6.5, "6", True])
async def test_values_must_be_whole_numbers_in_bounds(api: Api, value: object) -> None:
    async with api.client() as client:
        admin = await signed_in_admin(api, client)
        await api.step_up(client, admin)
        response = await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": value})
        assert response.status_code == 422


async def test_rules_between_settings_are_enforced(api: Api) -> None:
    async with api.client() as client:
        admin = await signed_in_admin(api, client)
        await api.step_up(client, admin)
        # The delay must start before the lock: threshold 5 with delay after 5 is refused.
        refused = await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": 5})
        assert refused.json()["errors"][0]["code"] == "setting.invalid.security.lockout.delay_after"


@pytest.mark.parametrize("key", ["smtp.password", "security.unknown.thing", "x"])
async def test_only_registry_keys_exist(api: Api, key: str) -> None:
    async with api.client() as client:
        admin = await signed_in_admin(api, client)
        await api.step_up(client, admin)
        assert (await client.put(f"/api/v1/settings/{key}", json={"value": 1})).status_code == 404


async def test_a_lower_lockout_threshold_applies_to_the_next_failures(api: Api) -> None:
    async with api.client() as client:
        admin = await signed_in_admin(api, client)
        await api.step_up(client, admin)
        await client.put("/api/v1/settings/security.lockout.delay_after", json={"value": 2})
        await client.put(f"/api/v1/settings/{THRESHOLD}", json={"value": 3})
    target = await api.create_account()
    async with api.client() as client:
        for _ in range(3):
            await client.post(
                "/api/v1/auth/login", json={"email": target.email, "password": "a wrong passphrase"}
            )
    [stored] = await api.records(User, {"id": target.user_id})
    assert stored.locked_until is not None


async def test_a_new_idle_timeout_applies_to_new_sessions(api: Api) -> None:
    async with api.client() as client:
        admin = await signed_in_admin(api, client)
        await api.step_up(client, admin)
        changed = await client.put(
            "/api/v1/settings/security.session.idle_timeout_minutes", json={"value": 5}
        )
        assert changed.status_code == 200, changed.text
    user = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, user)
    [session] = await api.records(AuthSession, {"user_id": user.user_id})
    assert (session.idle_expires_at - session.last_activity_at).total_seconds() == 300
