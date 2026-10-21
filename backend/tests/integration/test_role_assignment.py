"""Assigning and removing roles (docs/authorization-model.md §4-6): SoD rules, history, audit."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from pymongo.errors import WriteError

from app.modules.access.models import UserRole, role_id
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from app.platform.db import to_document
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

    rows = [
        (row.action, row.permission_used, (row.changes or {}).get("role"))
        for row in await api.records(
            AuditLogRecord, {"actor_user_id": admin.user_id}, sort=[("recorded_at", 1), ("id", 1)]
        )
    ]
    assert rows == [
        ("access.role.assigned", "role.assign", {"old": None, "new": "hr"}),
        ("access.role.removed", "role.assign", {"old": "hr", "new": None}),
    ]
    # History is kept: the removed assignment is revoked, not deleted.
    [stored] = await api.records(UserRole, {"id": uuid.UUID(assignment["id"])})
    assert stored.revoked_by == admin.user_id


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
        [own] = await api.records(UserRole, {"user_id": admin.user_id})
        assignment_id = own.id
        removal = await client.delete(f"/api/v1/users/{admin.user_id}/roles/{assignment_id}")
        assert removal.json()["code"] == "sod.own_roles"


async def test_the_database_refuses_a_self_granted_role(api: Api) -> None:
    account = await api.create_account()
    with pytest.raises(WriteError, match="Document failed validation"):
        await api.database.collection("user_roles").insert_one(
            stored_role(api, account.user_id, RoleKey.HR.value, granted_by=account.user_id)
        )


async def test_derived_and_unknown_roles_are_refused(api: Api) -> None:
    target = await api.create_account()
    async with api.client() as client:
        await super_admin(api, client)
        for role, status, code in (
            ("employee", 409, "role.derived"),
            ("manager", 409, "role.derived"),
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
    with pytest.raises(WriteError, match="Document failed validation"):
        await api.database.collection("user_roles").insert_one(
            stored_role(api, account.user_id, RoleKey.MANAGER.value)
        )


def stored_role(
    api: Api, user_id: uuid.UUID, key: str, *, granted_by: uuid.UUID | None = None
) -> dict[str, Any]:
    """A role assignment as stored, written past the application, so only the validator judges it."""
    return to_document(
        UserRole(
            organization_id=api.organization_id,
            user_id=user_id,
            role_id=role_id(key),
            role_key=key,
            granted_by=granted_by,
            valid_from=api.clock.now(),
            grant_reason="Test assignment",
        )
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
    # The organization is the test's own, so it holds exactly the three super admins below.
    async with api.client() as first_client, api.client() as second_client:
        await super_admin(api, first_client)
        second = await super_admin(api, second_client)
        third = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
        [third_role] = await api.records(UserRole, {"user_id": third.user_id})
        [second_role] = await api.records(UserRole, {"user_id": second.user_id})
        third_assignment, second_assignment = third_role.id, second_role.id
        # Two removals at once would leave one super admin: only one may succeed.
        results = await asyncio.gather(
            first_client.delete(f"/api/v1/users/{third.user_id}/roles/{third_assignment}"),
            first_client.delete(f"/api/v1/users/{second.user_id}/roles/{second_assignment}"),
        )
        assert sorted(r.status_code for r in results) == [204, 409]
        refused = next(r for r in results if r.status_code == 409)
        assert refused.json()["code"] == "role.min_super_admins"
    assert await api.count("user_roles", {"role_key": RoleKey.SUPER_ADMIN.value, "revoked_at": None}) == 2


async def test_removing_the_same_assignment_twice_at_once_is_clean(api: Api) -> None:
    """Locked reads see the current row, so the loser finds the assignment already revoked."""
    target = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    [held] = await api.records(UserRole, {"user_id": target.user_id})
    assignment_id = held.id
    async with api.client() as client:
        await super_admin(api, client)
        path = f"/api/v1/users/{target.user_id}/roles/{assignment_id}"
        results = await asyncio.gather(client.delete(path), client.delete(path), client.delete(path))
    assert sorted(r.status_code for r in results) == [204, 404, 404]
