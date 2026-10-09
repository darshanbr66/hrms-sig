"""Disabling administrator accounts (docs/authorization-model.md §6, decided for Checkpoint E).

An account holding `system_admin` or `super_admin` is disabled or enabled only by a
`system_admin` holder, so `user.disable` held for HR work cannot lock the administrators out.
Disabling a super admin, like removing the role, must leave at least two active super admins,
also under concurrent requests.
"""

import asyncio

import httpx
import pytest

from app.modules.identity.models import UserStatus
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api


@pytest.fixture
def api(isolated_api: Api) -> Api:
    # The super admin counts below must see only this test's super admins.
    return isolated_api


async def signed_in(api: Api, client: httpx.AsyncClient, role: RoleKey) -> Account:
    account = await api.create_account(roles=(role.value,))
    await api.sign_in(client, account)
    await api.step_up(client, account)
    return account


async def disable(client: httpx.AsyncClient, account: Account) -> httpx.Response:
    return await client.post(f"/api/v1/users/{account.user_id}/disable")


async def test_hr_cannot_disable_or_enable_an_administrator_account(api: Api) -> None:
    employee = await api.create_account()
    system_admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    super_admin = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    disabled_admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,), status=UserStatus.DISABLED)
    async with api.client() as client:
        await signed_in(api, client, RoleKey.HR)
        assert (await disable(client, employee)).status_code == 200
        for target in (system_admin, super_admin):
            refused = await disable(client, target)
            assert refused.status_code == 403
            assert refused.json()["code"] == "user.admin_account"
        refused = await client.post(f"/api/v1/users/{disabled_admin.user_id}/enable")
        assert refused.status_code == 403
        assert refused.json()["code"] == "user.admin_account"
    rows = await api.fetch(
        "SELECT id, status FROM identity.users WHERE id IN (:a, :b, :c)",
        a=system_admin.user_id,
        b=super_admin.user_id,
        c=disabled_admin.user_id,
    )
    assert dict(rows) == {
        system_admin.user_id: "active",
        super_admin.user_id: "active",
        disabled_admin.user_id: "disabled",
    }


async def test_a_system_admin_disables_administrators_but_keeps_two_active_super_admins(api: Api) -> None:
    other_admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    first, second, third = [await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,)) for _ in range(3)]
    async with api.client() as client:
        await signed_in(api, client, RoleKey.SYSTEM_ADMIN)
        assert (await disable(client, other_admin)).status_code == 200
        assert (await client.post(f"/api/v1/users/{other_admin.user_id}/enable")).status_code == 200

        assert (await disable(client, third)).status_code == 200
        # Two active super admins remain; neither can be disabled now.
        for target in (first, second):
            refused = await disable(client, target)
            assert refused.status_code == 409
            assert refused.json()["code"] == "user.min_super_admins"
        # Disabling an account that is already disabled changes nothing and is not refused.
        assert (await disable(client, third)).status_code == 200
        assert (await client.post(f"/api/v1/users/{third.user_id}/enable")).status_code == 200
        assert (await disable(client, first)).status_code == 200


async def test_concurrent_disables_cannot_leave_fewer_than_two_active_super_admins(api: Api) -> None:
    super_admins = [await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,)) for _ in range(3)]
    async with api.client() as first_client, api.client() as second_client:
        await signed_in(api, first_client, RoleKey.SYSTEM_ADMIN)
        await signed_in(api, second_client, RoleKey.SYSTEM_ADMIN)
        results = await asyncio.gather(
            disable(first_client, super_admins[0]), disable(second_client, super_admins[1])
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    [(active,)] = await api.fetch(
        "SELECT count(*) FROM identity.users WHERE status = 'active' AND id IN (:a, :b, :c)",
        a=super_admins[0].user_id,
        b=super_admins[1].user_id,
        c=super_admins[2].user_id,
    )
    assert active == 2


async def test_removing_the_role_counts_only_active_super_admins(api: Api) -> None:
    async with api.client() as client:
        await signed_in(api, client, RoleKey.SUPER_ADMIN)
        other = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
        await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,), status=UserStatus.DISABLED)
        [(assignment,)] = await api.fetch(
            "SELECT id FROM access.user_roles WHERE user_id = :id", id=other.user_id
        )
        # Three holders, but only two are active: removing one would leave one who can act.
        refused = await client.delete(f"/api/v1/users/{other.user_id}/roles/{assignment}")
        assert refused.status_code == 409
        assert refused.json()["code"] == "role.min_super_admins"
