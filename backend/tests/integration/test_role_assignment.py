"""Assigning and removing roles (docs/authorization-model.md §4-6): SoD rules, history, audit."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api


async def super_admin(api: Api, client: httpx.AsyncClient) -> Account:
    account = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    await api.sign_in(client, account)
    await api.step_up(client, account)
    return account


async def assign(client: httpx.AsyncClient, user_id: uuid.UUID, role: str, **extra: object) -> httpx.Response:
    return await client.post(
        f"/api/v1/users/{user_id}/roles", json={"role_key": role, "reason": "Covering HR", **extra}
    )


async def test_assigning_a_role_applies_rotates_sessions_and_is_audited(api: Api) -> None:
    target = await api.create_account()
    async with api.client() as admin_client, api.client() as target_client:
        admin = await super_admin(api, admin_client)
        await api.sign_in(target_client, target)
        assert (await target_client.get("/api/v1/users")).status_code == 403

        created = await assign(admin_client, target.user_id, "hr")
        assert created.status_code == 201, created.text
        assignment = created.json()
        assert assignment["role_key"] == "hr"
        assert assignment["granted_by"] == str(admin.user_id)

        # The target's access token was ended; a refresh issues new tokens with the new role.
        assert (await target_client.get("/api/v1/users")).status_code == 401
        assert (await target_client.post("/api/v1/auth/refresh")).status_code == 200
        assert (await target_client.get("/api/v1/users")).status_code == 200

        removed = await admin_client.delete(f"/api/v1/users/{target.user_id}/roles/{assignment['id']}")
        assert removed.status_code == 204
        await target_client.post("/api/v1/auth/refresh")
        assert (await target_client.get("/api/v1/users")).status_code == 403

    rows = await api.fetch(
        "SELECT action, permission_used, changes->'role' FROM audit.audit_log "
        "WHERE actor_user_id = :id ORDER BY recorded_at, id",
        id=admin.user_id,
    )
    assert rows == [
        ("access.role.assigned", "role.assign", {"old": None, "new": "hr"}),
        ("access.role.removed", "role.assign", {"old": "hr", "new": None}),
    ]
    # History is kept: the removed assignment is revoked, not deleted.
    [(revoked_by,)] = await api.fetch(
        "SELECT revoked_by FROM access.user_roles WHERE id = :id", id=uuid.UUID(assignment["id"])
    )
    assert revoked_by == admin.user_id


async def test_role_assignment_needs_step_up(api: Api) -> None:
    target = await api.create_account()
    account = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, account)
        response = await assign(client, target.user_id, "hr")
        assert response.status_code == 403
        assert response.json()["type"] == "/problems/step-up-required"


async def test_nobody_changes_their_own_roles(api: Api) -> None:
    async with api.client() as client:
        admin = await super_admin(api, client)
        response = await assign(client, admin.user_id, "auditor")
        assert response.status_code == 403
        assert response.json()["code"] == "sod.own_roles"
        [(assignment_id,)] = await api.fetch(
            "SELECT id FROM access.user_roles WHERE user_id = :id", id=admin.user_id
        )
        removal = await client.delete(f"/api/v1/users/{admin.user_id}/roles/{assignment_id}")
        assert removal.json()["code"] == "sod.own_roles"


async def test_the_database_refuses_a_self_granted_role(api: Api) -> None:
    account = await api.create_account()
    with pytest.raises(DBAPIError, match="ck_user_roles_not_self_granted"):
        await api.execute(
            "INSERT INTO access.user_roles (user_id, role_id, granted_by, grant_reason) "
            "SELECT :id, id, :id, 'Self' FROM access.roles WHERE key = 'hr'",
            id=account.user_id,
        )


async def test_derived_super_admin_and_unknown_roles_are_refused(api: Api) -> None:
    target = await api.create_account()
    async with api.client() as client:
        await super_admin(api, client)
        for role, status, code in (
            ("employee", 409, "role.derived"),
            ("manager", 409, "role.derived"),
            ("super_admin", 409, "sod.super_admin_assignment"),
        ):
            response = await assign(client, target.user_id, role)
            assert response.status_code == status, role
            assert response.json()["code"] == code
        unknown = await assign(client, target.user_id, "owner")
        assert unknown.status_code == 422
        missing = await assign(client, uuid.uuid4(), "hr")
        assert missing.status_code == 404
        bad_department = await assign(client, target.user_id, "hr", department_id=str(uuid.uuid4()))
        assert bad_department.status_code == 422
        past = await assign(client, target.user_id, "hr", valid_until="2000-01-01T00:00:00Z")
        assert past.status_code == 422
        naive = await assign(client, target.user_id, "hr", valid_until="2099-01-01T00:00:00")
        assert naive.status_code == 422


async def test_the_database_refuses_a_stored_derived_role(api: Api) -> None:
    account = await api.create_account()
    with pytest.raises(DBAPIError, match="fk_user_roles_role_id_role_is_derived"):
        await api.execute(
            "INSERT INTO access.user_roles (user_id, role_id, grant_reason) "
            "SELECT :id, id, 'Derived' FROM access.roles WHERE key = 'manager'",
            id=account.user_id,
        )


async def test_a_super_admin_holds_no_standing_data_role(api: Api) -> None:
    """SOD-10: data roles reach a super admin only through time-bound elevation."""
    other_super_admin = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    async with api.client() as client:
        await super_admin(api, client)
        for role in ("hr", "hr_admin", "payroll_admin", "system_admin", "auditor"):
            response = await assign(client, other_super_admin.user_id, role)
            assert response.json()["code"] == "sod.super_admin_data_role", role


async def test_an_active_assignment_cannot_be_duplicated(api: Api) -> None:
    target = await api.create_account()
    async with api.client() as client:
        await super_admin(api, client)
        results = await asyncio.gather(*(assign(client, target.user_id, "auditor") for _ in range(3)))
        statuses = sorted(r.status_code for r in results)
        assert statuses == [201, 409, 409]


async def test_at_least_two_super_admins_remain_even_under_concurrent_removals(api: Api) -> None:
    # Start from exactly the three super admins of this test (earlier tests in the shared
    # database created others); revoking is the normal way to end an assignment.
    await api.execute(
        "UPDATE access.user_roles SET revoked_at = now() WHERE revoked_at IS NULL AND role_id = "
        "(SELECT id FROM access.roles WHERE key = 'super_admin')"
    )
    async with api.client() as first_client, api.client() as second_client:
        await super_admin(api, first_client)
        second = await super_admin(api, second_client)
        third = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
        [(third_assignment,)] = await api.fetch(
            "SELECT id FROM access.user_roles WHERE user_id = :id", id=third.user_id
        )
        [(second_assignment,)] = await api.fetch(
            "SELECT id FROM access.user_roles WHERE user_id = :id", id=second.user_id
        )
        # Two removals at once would leave one super admin: only one may succeed.
        results = await asyncio.gather(
            first_client.delete(f"/api/v1/users/{third.user_id}/roles/{third_assignment}"),
            first_client.delete(f"/api/v1/users/{second.user_id}/roles/{second_assignment}"),
        )
        assert sorted(r.status_code for r in results) == [204, 409]
        refused = next(r for r in results if r.status_code == 409)
        assert refused.json()["code"] == "role.min_super_admins"
    [(holders,)] = await api.fetch(
        "SELECT count(*) FROM access.user_roles ur JOIN access.roles r ON r.id = ur.role_id "
        "WHERE r.key = 'super_admin' AND ur.revoked_at IS NULL"
    )
    assert holders == 2


async def test_removing_the_same_assignment_twice_at_once_is_clean(api: Api) -> None:
    """Locked reads see the current row, so the loser finds the assignment already revoked."""
    target = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    [(assignment_id,)] = await api.fetch(
        "SELECT id FROM access.user_roles WHERE user_id = :id", id=target.user_id
    )
    async with api.client() as client:
        await super_admin(api, client)
        path = f"/api/v1/users/{target.user_id}/roles/{assignment_id}"
        results = await asyncio.gather(client.delete(path), client.delete(path), client.delete(path))
    assert sorted(r.status_code for r in results) == [204, 404, 404]
