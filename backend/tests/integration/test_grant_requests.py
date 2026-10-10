"""Role grant requests (docs/authorization-model.md §4.3, §6; threat model T6, T6a).

Elevation: requested with step-up, approved by a different super admin with step-up, applies
for exactly its window and stops applying at its end without any job; every action under it
carries its ID in the audit log. Super admin assignment needs a second super admin who is
neither assigner nor assignee (SOD-8) and an assignee without standing data roles (SOD-10).
Break-glass reaches only `system_admin` for at most an hour, alerts every super admin and
auditor, and is acknowledged by another super admin. The database enforces the limits too.

Each test runs on a database of its own: every grant event emails every super admin, and the
shared test database holds the super admins other tests create.
"""

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app.modules.access.grant_requests import expire_due
from app.platform.audit.writer import AuditWriter
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api

REQUESTS = "/api/v1/role-grant-requests"


@pytest.fixture
def api(isolated_api: Api) -> Api:
    return isolated_api


async def super_admin(api: Api, client: httpx.AsyncClient) -> Account:
    account = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    await api.sign_in(client, account)
    await api.step_up(client, account)
    return account


async def request_elevation(
    client: httpx.AsyncClient, role: str = "system_admin", **extra: object
) -> httpx.Response:
    return await client.post(REQUESTS, json={"role_key": role, "reason": "Fix a stuck account", **extra})


async def can_read_sessions(client: httpx.AsyncClient, user_id: uuid.UUID) -> int:
    """`auth.session.read.all` belongs to system_admin, not super_admin."""
    return (await client.get(f"/api/v1/users/{user_id}/sessions")).status_code


async def expire(api: Api) -> None:
    await expire_due(api.worker_database, api.clock, AuditWriter(api.clock))


async def test_an_elevation_is_approved_by_another_super_admin_and_ends_on_time(api: Api) -> None:
    async with api.client() as requester_client, api.client() as approver_client:
        requester = await super_admin(api, requester_client)
        approver = await super_admin(api, approver_client)
        assert await can_read_sessions(requester_client, approver.user_id) == 403

        created = await request_elevation(requester_client, duration_minutes=5)
        assert created.status_code == 201, created.text
        request = created.json()
        assert request["status"] == "pending"
        assert request["duration_minutes"] == 5
        assert request["subject"] == {"id": str(requester.user_id), "email": requester.email}
        assert request["decide_by"] is not None

        # SOD-9: the requester cannot approve their own request.
        own = await requester_client.post(f"{REQUESTS}/{request['id']}/approve", json={})
        assert own.status_code == 403
        assert own.json()["code"] == "sod.own_request"

        approved = await approver_client.post(f"{REQUESTS}/{request['id']}/approve", json={"note": "Checked"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "active"
        assert approved.json()["decided_by"]["id"] == str(approver.user_id)

        # The requester's access token was ended; the refreshed session holds the role.
        assert (await requester_client.get("/api/v1/me")).status_code == 401
        assert (await requester_client.post("/api/v1/auth/refresh")).status_code == 200
        assert await can_read_sessions(requester_client, approver.user_id) == 200
        banner = (await requester_client.get("/api/v1/me/elevation")).json()["elevation"]
        assert banner["id"] == request["id"]
        assert banner["role_key"] == "system_admin"

        # Every action under the elevation carries its ID in the audit log.
        [(grant_request_id,)] = await api.fetch(
            "SELECT grant_request_id FROM audit.audit_log WHERE actor_user_id = :id "
            "AND action = 'identity.sessions.read'",
            id=requester.user_id,
        )
        assert str(grant_request_id) == request["id"]

        # It stops applying at its end, before any job runs.
        api.clock.advance(timedelta(minutes=5, seconds=1))
        assert await can_read_sessions(requester_client, approver.user_id) == 403
        assert (await requester_client.get("/api/v1/me/elevation")).json() == {"elevation": None}
        listed = (await approver_client.get(REQUESTS)).json()["items"]
        assert next(item for item in listed if item["id"] == request["id"])["status"] == "ended"

    # The job records the end and closes the assignment.
    await expire(api)
    [(status,)] = await api.fetch(
        "SELECT status FROM access.role_grant_requests WHERE id = :id", id=request["id"]
    )
    assert status == "ended"
    [(revoked_at, valid_until)] = await api.fetch(
        "SELECT revoked_at, valid_until FROM access.user_roles WHERE grant_request_id = :id", id=request["id"]
    )
    assert revoked_at == valid_until

    actions = [
        action
        for (action,) in await api.fetch(
            "SELECT action FROM audit.audit_log WHERE target_id = :id ORDER BY recorded_at, id",
            id=request["id"],
        )
    ]
    assert actions == ["access.elevation.requested", "access.elevation.approved", "access.elevation.ended"]
    events = [
        event
        for (event,) in await api.fetch(
            "SELECT event_type FROM audit.security_events WHERE details->>'grant_request_id' = :id "
            "ORDER BY recorded_at, id",
            id=request["id"],
        )
    ]
    assert events == ["elevation.requested", "elevation.approved", "elevation.ended"]

    # Every super admin hears about each step, except the one who acted.
    sent = await api.deliver()
    to_approver = [m.subject for m in sent if m.to == approver.email]
    to_requester = [m.subject for m in sent if m.to == requester.email]
    assert any("needs approval" in subject for subject in to_approver)
    assert any("was approved" in subject for subject in to_requester)
    assert all("Fix a stuck account" not in m.body for m in sent)


async def test_requesting_and_approving_need_step_up(api: Api) -> None:
    async with api.client() as client:
        account = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
        await api.sign_in(client, account)
        refused = await request_elevation(client)
        assert refused.json()["type"] == "/problems/step-up-required"


async def test_a_request_not_decided_within_24_hours_expires(api: Api) -> None:
    async with api.client() as requester_client:
        await super_admin(api, requester_client)
        request = (await request_elevation(requester_client)).json()
    api.clock.advance(timedelta(hours=24))
    async with api.client() as approver_client:
        await super_admin(api, approver_client)
        late = await approver_client.post(f"{REQUESTS}/{request['id']}/approve", json={})
        assert late.status_code == 409
        assert late.json()["code"] == "grant_request.not_pending"
    await expire(api)
    [(status,)] = await api.fetch(
        "SELECT status FROM access.role_grant_requests WHERE id = :id", id=request["id"]
    )
    assert status == "expired"


async def test_reject_withdraw_and_end_early(api: Api) -> None:
    async with api.client() as requester_client, api.client() as other_client:
        requester = await super_admin(api, requester_client)
        other = await super_admin(api, other_client)

        first = (await request_elevation(requester_client)).json()
        # One pending elevation at a time per person.
        again = await request_elevation(requester_client, role="auditor")
        assert again.status_code == 409
        assert again.json()["code"] == "grant_request.pending_exists"
        rejected = await other_client.post(f"{REQUESTS}/{first['id']}/reject", json={"note": "Not needed"})
        assert rejected.json()["status"] == "rejected"
        assert (await other_client.post(f"{REQUESTS}/{first['id']}/approve", json={})).status_code == 409

        second = (await request_elevation(requester_client)).json()
        withdrawn = await requester_client.post(f"{REQUESTS}/{second['id']}/revoke")
        assert withdrawn.json()["status"] == "revoked"

        third = (await request_elevation(requester_client)).json()
        await other_client.post(f"{REQUESTS}/{third['id']}/approve", json={})
        await requester_client.post("/api/v1/auth/refresh")
        assert await can_read_sessions(requester_client, other.user_id) == 200
        # The assignment is ended through its request, never removed as a role.
        [(assignment_id,)] = await api.fetch(
            "SELECT id FROM access.user_roles WHERE grant_request_id = :id", id=third["id"]
        )
        removal = await other_client.delete(f"/api/v1/users/{requester.user_id}/roles/{assignment_id}")
        assert removal.json()["code"] == "role.temporary_access"
        ended = await other_client.post(f"{REQUESTS}/{third['id']}/revoke")
        assert ended.json()["status"] == "revoked"
        await requester_client.post("/api/v1/auth/refresh")
        assert await can_read_sessions(requester_client, other.user_id) == 403

        # The same role can be given again after the earlier assignment ended.
        fourth = (await request_elevation(requester_client)).json()
        assert (await other_client.post(f"{REQUESTS}/{fourth['id']}/approve", json={})).status_code == 200


async def test_elevation_limits(api: Api) -> None:
    async with api.client() as client:
        await super_admin(api, client)
        for minutes in (481, 0):
            assert (await request_elevation(client, duration_minutes=minutes)).status_code == 422
        for role in ("super_admin", "employee", "manager", "owner"):
            refused = await request_elevation(client, role=role)
            assert refused.status_code == 422, role
            assert refused.json()["errors"][0]["code"] == "grant_request.role_not_elevatable"


async def test_break_glass_reaches_only_system_admin_for_an_hour_and_needs_review(api: Api) -> None:
    auditor = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    async with api.client() as client, api.client() as other_client:
        user = await super_admin(api, client)
        other = await super_admin(api, other_client)
        assert (
            await client.post(f"{REQUESTS}/break-glass", json={"reason": "x", "duration_minutes": 61})
        ).status_code == 422
        # The body has no role: break-glass is always system_admin.
        extra = await client.post(f"{REQUESTS}/break-glass", json={"reason": "x", "role_key": "hr_admin"})
        assert extra.status_code == 422

        used = await client.post(f"{REQUESTS}/break-glass", json={"reason": "Both admins locked out"})
        assert used.status_code == 201, used.text
        glass = used.json()
        assert (glass["kind"], glass["status"], glass["role_key"], glass["duration_minutes"]) == (
            "break_glass",
            "active",
            "system_admin",
            60,
        )
        await client.post("/api/v1/auth/refresh")
        assert await can_read_sessions(client, other.user_id) == 200
        # One temporary access at a time.
        assert (await client.post(f"{REQUESTS}/break-glass", json={"reason": "Again"})).json()["code"] == (
            "grant_request.already_elevated"
        )
        # Nobody reviews their own break-glass use.
        await api.step_up(client, user)
        own = await client.post(f"{REQUESTS}/{glass['id']}/acknowledge")
        assert own.json()["code"] == "sod.own_request"
        # The review is post-incident: it cannot be signed off while the access still applies.
        early = await other_client.post(f"{REQUESTS}/{glass['id']}/acknowledge")
        assert early.status_code == 409
        assert early.json()["code"] == "grant_request.still_active"
        ended = await other_client.post(f"{REQUESTS}/{glass['id']}/revoke")
        assert ended.json()["status"] == "revoked"
        reviewed = await other_client.post(f"{REQUESTS}/{glass['id']}/acknowledge")
        assert reviewed.status_code == 200
        assert reviewed.json()["acknowledged_by"]["id"] == str(other.user_id)
        assert (await other_client.post(f"{REQUESTS}/{glass['id']}/acknowledge")).status_code == 409

    [(severity,)] = await api.fetch(
        "SELECT severity FROM audit.security_events WHERE event_type = 'elevation.break_glass' "
        "AND details->>'grant_request_id' = :id",
        id=glass["id"],
    )
    assert severity == "high"
    sent = await api.deliver()
    assert any("Break-glass access was used" in m.subject for m in sent if m.to == auditor.email)
    assert any("Break-glass access was used" in m.subject for m in sent if m.to == other.email)
    # Its user is not alerted to their own use, but is told another super admin ended it.
    to_user = [m.subject for m in sent if m.to == user.email]
    assert not any("Break-glass access was used" in subject for subject in to_user)
    assert any("Break-glass access was ended" in subject for subject in to_user)


async def test_super_admin_assignment_needs_a_second_super_admin(api: Api) -> None:
    candidate = await api.create_account()
    async with api.client() as assigner_client, api.client() as approver_client:
        assigner = await super_admin(api, assigner_client)
        await super_admin(api, approver_client)
        created = await assigner_client.post(
            f"/api/v1/users/{candidate.user_id}/roles",
            json={"role_key": "super_admin", "reason": "Third admin"},
        )
        assert created.status_code == 202, created.text
        request = created.json()
        assert (request["kind"], request["status"]) == ("super_admin_assignment", "pending")
        assert (
            await api.fetch("SELECT 1 FROM access.user_roles WHERE user_id = :id", id=candidate.user_id)
        ) == []

        own = await assigner_client.post(f"{REQUESTS}/{request['id']}/approve", json={})
        assert own.json()["code"] == "sod.own_request"
        approved = await approver_client.post(f"{REQUESTS}/{request['id']}/approve", json={})
        assert approved.json()["status"] == "approved"
        roles = (await assigner_client.get(f"/api/v1/users/{candidate.user_id}/roles")).json()["items"]
        assert [(r["role_key"], r["grant_request_id"]) for r in roles] == [("super_admin", request["id"])]
        assert roles[0]["granted_by"] != str(assigner.user_id)


async def test_a_super_admin_assignment_respects_sod_10(api: Api) -> None:
    candidate = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await super_admin(api, client)
        refused = await client.post(
            f"/api/v1/users/{candidate.user_id}/roles", json={"role_key": "super_admin", "reason": "Admin"}
        )
        assert refused.status_code == 409
        assert refused.json()["code"] == "sod.super_admin_data_role"
        scoped = await client.post(
            f"/api/v1/users/{candidate.user_id}/roles",
            json={"role_key": "super_admin", "reason": "Admin", "valid_until": "2099-01-01T00:00:00Z"},
        )
        assert scoped.status_code == 422


async def test_listing_needs_role_read_and_is_newest_first(api: Api) -> None:
    async with api.client() as client, api.client() as auditor_client:
        await super_admin(api, client)
        first = (await request_elevation(client)).json()
        await client.post(f"{REQUESTS}/{first['id']}/revoke")
        second = (await request_elevation(client, role="auditor")).json()
        auditor = await api.create_account(roles=(RoleKey.AUDITOR.value,))
        await api.sign_in(auditor_client, auditor)
        page = (await auditor_client.get(REQUESTS, params={"limit": 1})).json()
        assert [item["id"] for item in page["items"]] == [second["id"]]
        rest = (await auditor_client.get(REQUESTS, params={"cursor": page["next_cursor"]})).json()
        assert [item["id"] for item in rest["items"]] == [first["id"]]
        pending = (await auditor_client.get(REQUESTS, params={"status": "pending"})).json()["items"]
        assert [item["id"] for item in pending] == [second["id"]]
        # Auditors read; they do not decide.
        assert (await auditor_client.post(f"{REQUESTS}/{second['id']}/approve", json={})).status_code == 403
    async with api.client() as employee_client:
        await api.sign_in(employee_client, await api.create_account())
        assert (await employee_client.get(REQUESTS)).status_code == 403


async def test_the_database_enforces_the_grant_request_rules(api: Api) -> None:
    subject = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    other = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))

    def insert(kind: str, role: str, status: str, extra: str = "", values: str = "") -> str:
        return (
            "INSERT INTO access.role_grant_requests (kind, subject_user_id, role_id, role_key, reason, "
            f"requested_duration, requested_by, status{extra}) SELECT '{kind}', :s, id, key, 'r', "
            f":d, :s, '{status}'{values} FROM access.roles WHERE key = '{role}' RETURNING id"
        )

    for statement, duration, match in (
        (insert("elevation", "hr", "pending"), timedelta(hours=9), "ck_role_grant_requests_duration"),
        (insert("break_glass", "hr", "pending"), timedelta(minutes=30), "ck_role_grant_requests"),
        (
            insert(
                "break_glass",
                "system_admin",
                "active",
                ", starts_at, ends_at",
                ", now(), now() + interval '2 hours'",
            ),
            timedelta(hours=2),
            "ck_role_grant_requests_duration",
        ),
        (insert("elevation", "super_admin", "pending"), timedelta(hours=1), "ck_role_grant_requests_role"),
    ):
        with pytest.raises(DBAPIError, match=match):
            await api.execute(statement, s=subject.user_id, d=duration)

    [(request_id,)] = await api.fetch(
        insert("elevation", "hr", "pending"), s=subject.user_id, d=timedelta(hours=1)
    )
    # Self-approval, changing what was asked, going backwards and deleting are all refused.
    with pytest.raises(DBAPIError, match="ck_role_grant_requests_decider"):
        await api.execute(
            "UPDATE access.role_grant_requests SET status = 'rejected', decided_by = requested_by, "
            "decided_at = now() WHERE id = :id",
            id=request_id,
        )
    with pytest.raises(DBAPIError, match="cannot change"):
        await api.execute(
            "UPDATE access.role_grant_requests SET reason = 'other' WHERE id = :id", id=request_id
        )
    with pytest.raises(DBAPIError, match="history"):
        await api.execute("DELETE FROM access.role_grant_requests WHERE id = :id", id=request_id)
    await api.execute(
        "UPDATE access.role_grant_requests SET status = 'rejected', decided_by = :o, decided_at = now() "
        "WHERE id = :id",
        id=request_id,
        o=other.user_id,
    )
    with pytest.raises(DBAPIError, match="cannot move"):
        await api.execute(
            "UPDATE access.role_grant_requests SET status = 'pending' WHERE id = :id", id=request_id
        )

    # An assignment must match an approved request: a rejected one grants nothing, and
    # super_admin is never assigned by someone without a request.
    with pytest.raises(DBAPIError, match="does not allow"):
        await api.execute(
            "INSERT INTO access.user_roles (user_id, role_id, granted_by, grant_reason, grant_request_id, "
            "valid_until) SELECT :s, id, :o, 'r', :r, now() + interval '1 hour' "
            "FROM access.roles WHERE key = 'hr'",
            s=subject.user_id,
            o=other.user_id,
            r=request_id,
        )
    candidate = await api.create_account()
    with pytest.raises(DBAPIError, match="SOD-8"):
        await api.execute(
            "INSERT INTO access.user_roles (user_id, role_id, granted_by, grant_reason) "
            "SELECT :c, id, :o, 'r' FROM access.roles WHERE key = 'super_admin'",
            c=candidate.user_id,
            o=other.user_id,
        )


async def test_removing_a_super_admin_ends_their_temporary_access(api: Api) -> None:
    async with api.client() as requester_client, api.client() as other_client:
        requester = await super_admin(api, requester_client)
        other = await super_admin(api, other_client)
        await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))  # two remain afterwards
        request = (await request_elevation(requester_client)).json()
        await other_client.post(f"{REQUESTS}/{request['id']}/approve", json={})
        await requester_client.post("/api/v1/auth/refresh")
        assert await can_read_sessions(requester_client, other.user_id) == 200

        [(assignment_id,)] = await api.fetch(
            "SELECT ur.id FROM access.user_roles ur JOIN access.roles r ON r.id = ur.role_id "
            "WHERE ur.user_id = :id AND r.key = 'super_admin'",
            id=requester.user_id,
        )
        removed = await other_client.delete(f"/api/v1/users/{requester.user_id}/roles/{assignment_id}")
        assert removed.status_code == 204
        await requester_client.post("/api/v1/auth/refresh")
        assert await can_read_sessions(requester_client, other.user_id) == 403
    [(status,)] = await api.fetch(
        "SELECT status FROM access.role_grant_requests WHERE id = :id", id=request["id"]
    )
    assert status == "revoked"
