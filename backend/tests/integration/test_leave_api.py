"""Leave management over HTTP (docs/api-architecture.md §8 Leave, authorization-model §3.4).

Every leave type and policy here is test configuration, not Sigvitas policy. All data is
obviously fake (docs/database-design.md §10).
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.people_data import OrgUnits, add_job, create_employee, create_org_units, fake_code


@dataclass
class World:
    units: OrgUnits
    admin: Account
    hr: Account
    manager: Account
    employee: Account
    stranger_manager: Account
    leave_type_id: str
    start: date


def type_body(**overrides: Any) -> dict[str, Any]:
    return {
        "code": fake_code("LT"),
        "name": "Test leave",
        "is_paid": True,
        "requires_approval": True,
        "allows_half_day": True,
        "counts_weekly_offs": True,
        "counts_holidays": False,
        **overrides,
    }


def policy_body(leave_type_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "leave_type_id": leave_type_id,
        "effective_from": "2020-01-01",
        "leave_year_start_month": 1,
        "leave_year_start_day": 1,
        "accrual_method": "upfront_per_leave_year",
        "accrual_amount": "5",
        "allow_negative_balance": False,
        **overrides,
    }


async def world(api: Api, *, type_overrides: dict[str, Any] | None = None, **policy: Any) -> World:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        manager_id = await create_employee(session, joined=today - timedelta(days=400))
        await add_job(session, units, manager_id, manager_id=None, effective_from=today - timedelta(days=400))
        employee_id = await create_employee(session, joined=today - timedelta(days=300))
        await add_job(
            session, units, employee_id, manager_id=manager_id, effective_from=today - timedelta(days=300)
        )
        other_id = await create_employee(session, joined=today - timedelta(days=300))
        await add_job(session, units, other_id, manager_id=None, effective_from=today - timedelta(days=300))
        stray_report = await create_employee(session, joined=today - timedelta(days=300))
        await add_job(
            session, units, stray_report, manager_id=other_id, effective_from=today - timedelta(days=300)
        )
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, admin)
        created = await client.post("/api/v1/leave-types", json=type_body(**(type_overrides or {})))
        assert created.status_code == 201, created.text
        leave_type_id = created.json()["id"]
        made = await client.post(
            "/api/v1/leave-policies",
            json=policy_body(leave_type_id, location_id=str(units.location_id), **policy),
        )
        assert made.status_code == 201, made.text
    return World(
        units=units,
        admin=admin,
        hr=await api.create_account(roles=(RoleKey.HR.value,)),
        manager=await api.create_account(employee_id=manager_id),
        employee=await api.create_account(employee_id=employee_id),
        stranger_manager=await api.create_account(employee_id=other_id),
        leave_type_id=leave_type_id,
        start=today + timedelta(days=40),
    )


async def allocate(api: Api, w: World) -> dict[str, int]:
    async with api.client() as client:
        await api.sign_in(client, w.hr)
        result = await client.post(
            "/api/v1/leave-allocations",
            json={"leave_type_id": w.leave_type_id, "as_of": api.clock.now().date().isoformat()},
        )
        assert result.status_code == 200, result.text
        return dict(result.json())


def draft(w: World, days: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "leave_type_id": w.leave_type_id,
        "start_date": w.start.isoformat(),
        "end_date": (w.start + timedelta(days=days - 1)).isoformat(),
        **extra,
    }


async def another_employee(api: Api, w: World, *, roles: tuple[str, ...] = ()) -> Account:
    """One more employee in the world's units, reporting to its manager, with an account."""
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        employee = await create_employee(session, joined=today - timedelta(days=200))
        await add_job(
            session,
            w.units,
            employee,
            manager_id=w.manager.employee_id,
            effective_from=today - timedelta(days=200),
        )
    return await api.create_account(roles=roles, employee_id=employee)


def key() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid.uuid4()}"}


async def balance(
    client: httpx.AsyncClient, w: World, path: str = "/api/v1/me/leave/balances"
) -> dict[str, Any]:
    items = (await client.get(path)).json()["items"]
    return next(item for item in items if item["leave_type"]["id"] == w.leave_type_id)


# --- configuration --------------------------------------------------------------------------------


async def test_leave_types_and_policies_are_configured_by_hr_admins_only(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, hr)
        assert (await client.post("/api/v1/leave-types", json=type_body())).status_code == 403
    async with api.client() as client:
        await api.sign_in(client, admin)
        missing = type_body()
        del missing["requires_approval"]
        assert (await client.post("/api/v1/leave-types", json=missing)).status_code == 422
        body = type_body()
        created = await client.post("/api/v1/leave-types", json=body)
        assert created.status_code == 201
        leave_type = created.json()
        clash = await client.post("/api/v1/leave-types", json={**type_body(), "code": body["code"].lower()})
        assert clash.status_code == 409
        assert clash.json()["code"] == "leave.type_code_in_use"

        bad_year = await client.post(
            "/api/v1/leave-policies",
            json=policy_body(leave_type["id"], leave_year_start_month=2, leave_year_start_day=29),
        )
        assert bad_year.json()["errors"][0]["code"] == "leave.year_start_invalid"
        no_amount = await client.post(
            "/api/v1/leave-policies", json=policy_body(leave_type["id"], accrual_amount=None)
        )
        assert no_amount.json()["errors"][0]["code"] == "leave.accrual_amount"
        no_limit = await client.post(
            "/api/v1/leave-policies", json=policy_body(leave_type["id"], allow_negative_balance=True)
        )
        assert no_limit.json()["errors"][0]["code"] == "leave.max_negative_days"

        first = await client.post("/api/v1/leave-policies", json=policy_body(leave_type["id"]))
        assert first.status_code == 201, first.text
        second = await client.post(
            "/api/v1/leave-policies",
            json=policy_body(leave_type["id"], effective_from="2025-01-01", accrual_amount="7.5"),
        )
        assert second.status_code == 201, second.text
        earlier = await client.post(
            "/api/v1/leave-policies", json=policy_body(leave_type["id"], effective_from="2024-06-01")
        )
        assert earlier.status_code == 409
        assert earlier.json()["code"] == "leave.policy_overlap"
        versions = (
            await client.get("/api/v1/leave-policies", params={"leave_type_id": leave_type["id"]})
        ).json()["items"]
        assert [(v["effective_from"], v["effective_to"], v["accrual_amount"]) for v in versions] == [
            ("2025-01-01", None, "7.50"),
            ("2020-01-01", "2025-01-01", "5.00"),
        ]

        renamed = await client.patch(
            f"/api/v1/leave-types/{leave_type['id']}", json={"name": "Renamed"}, headers={"If-Match": '"1"'}
        )
        assert renamed.json()["version"] == 2
        stale = await client.patch(
            f"/api/v1/leave-types/{leave_type['id']}", json={"name": "Again"}, headers={"If-Match": '"1"'}
        )
        assert stale.status_code == 412

    actions = await api.fetch(
        "SELECT action FROM audit.audit_log WHERE action LIKE 'leave.%' AND actor_user_id = :id "
        "ORDER BY recorded_at, id",
        id=admin.user_id,
    )
    assert [a for (a,) in actions] == [
        "leave.type.created",
        "leave.policy.created",
        "leave.policy.created",
        "leave.type.updated",
    ]
    with pytest.raises(DBAPIError):
        await api.execute(
            "UPDATE leave.leave_policies SET accrual_amount = 99 WHERE leave_type_id = :id",
            id=leave_type["id"],
        )


# --- allocation and adjustments --------------------------------------------------------------------


async def test_allocation_gives_each_eligible_employee_the_policy_amount_once(api: Api) -> None:
    w = await world(api)
    first = await allocate(api, w)
    assert first["allocated"] >= 4
    again = await allocate(api, w)
    assert again["allocated"] == 0
    assert again["already_allocated"] == first["allocated"]
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        mine = await balance(client, w)
        assert (mine["balance"], mine["reserved"], mine["available"], mine["eligible"]) == (
            "5.00",
            "0.00",
            "5.00",
            True,
        )
    entries = await api.fetch(
        "SELECT entry_type, amount FROM leave.leave_ledger WHERE employee_id = :id",
        id=w.employee.employee_id,
    )
    assert [(e, str(a)) for e, a in entries] == [("accrual", "5.00")]


async def test_hr_adjusts_with_a_reason_never_their_own_balance(api: Api) -> None:
    w = await world(api)
    hr_with_record = await another_employee(api, w, roles=(RoleKey.HR.value,))
    hr_employee = hr_with_record.employee_id
    path = f"/api/v1/employees/{w.employee.employee_id}/leave-adjustments"
    body = {
        "leave_type_id": w.leave_type_id,
        "entry_type": "opening_balance",
        "amount": "2.5",
        "effective_date": api.clock.now().date().isoformat(),
        "reason": "Test: balance carried from the old system",
    }
    async with api.client() as client:
        await api.sign_in(client, w.hr)
        headers = key()
        made = await client.post(path, json=body, headers=headers)
        assert made.status_code == 201, made.text
        replay = await client.post(path, json=body, headers=headers)
        assert replay.json()["id"] == made.json()["id"]
        twice = await client.post(path, json=body, headers=key())
        assert twice.status_code == 409
        assert twice.json()["code"] == "leave.opening_balance_exists"
        assert (await client.post(path, json={**body, "amount": "0.3"}, headers=key())).status_code == 422
        minus = await client.post(
            path, json={**body, "entry_type": "adjustment", "amount": "-1"}, headers=key()
        )
        assert minus.status_code == 201
        got = await balance(client, w, f"/api/v1/employees/{w.employee.employee_id}/leave/balances")
        assert got["balance"] == "1.50"
    async with api.client() as client:
        await api.sign_in(client, hr_with_record)
        own = await client.post(
            f"/api/v1/employees/{hr_employee}/leave-adjustments", json=body, headers=key()
        )
        assert own.status_code == 403
    async with api.client() as client:
        await api.sign_in(client, w.manager)
        assert (await client.post(path, json=body, headers=key())).status_code == 403
    [(reason,)] = await api.fetch(
        "SELECT reason FROM audit.audit_log WHERE action = 'leave.balance.adjusted' "
        "AND subject_employee_id = :id ORDER BY recorded_at LIMIT 1",
        id=w.employee.employee_id,
    )
    assert reason == body["reason"]
    with pytest.raises(DBAPIError):
        await api.execute("DELETE FROM leave.leave_ledger WHERE employee_id = :id", id=w.employee.employee_id)


# --- the request lifecycle --------------------------------------------------------------------------


async def submit_reserving(api: Api, w: World) -> dict[str, Any]:
    """The employee previews and submits two days; a retry, the reservation and an overlap."""
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        preview = await client.post("/api/v1/me/leave-requests/preview", json=draft(w, 2))
        assert preview.status_code == 200, preview.text
        assert preview.json()["days_requested"] == "2.00"
        assert preview.json()["weekly_offs_configured"] is False
        headers = key()
        submitted = await client.post(
            "/api/v1/me/leave-requests", json={**draft(w, 2), "reason": "Test family event"}, headers=headers
        )
        assert submitted.status_code == 201, submitted.text
        request = submitted.json()
        assert request["status"] == "pending"
        assert request["reason"] == "Test family event"
        replay = await client.post(
            "/api/v1/me/leave-requests", json={**draft(w, 2), "reason": "Test family event"}, headers=headers
        )
        assert replay.json()["id"] == request["id"]
        mine = await balance(client, w)
        assert (mine["balance"], mine["reserved"], mine["available"]) == ("5.00", "2.00", "3.00")
        overlap = await client.post("/api/v1/me/leave-requests", json=draft(w, 1), headers=key())
        assert overlap.status_code == 409
        assert overlap.json()["code"] == "leave.overlap"
    return dict(request)


async def test_a_request_reserves_days_and_the_manager_approves_it(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    request = await submit_reserving(api, w)
    outbox = await api.fetch(
        "SELECT template_data FROM notify.email_outbox WHERE user_id = :id AND template = 'leave.request'",
        id=w.manager.user_id,
    )
    assert len(outbox) == 1
    assert outbox[0][0]["event"] == "submitted"
    assert "Test family event" not in str(outbox[0][0])

    path = f"/api/v1/leave-requests/{request['id']}"
    async with api.client() as client:
        await api.sign_in(client, w.stranger_manager)
        assert (await client.get(path)).status_code == 404
        assert (await client.post(f"{path}/approve", json={}, headers=key())).status_code == 404
    async with api.client() as client:
        await api.sign_in(client, w.manager)
        inbox = (await client.get("/api/v1/leave-requests", params={"awaiting_my_decision": "true"})).json()
        assert [item["id"] for item in inbox["items"]] == [request["id"]]
        assert "reason" not in inbox["items"][0]
        seen = (await client.get(path)).json()
        assert (seen["reason"], seen["can_decide"]) == ("Test family event", True)
        no_note = await client.post(f"{path}/reject", json={}, headers=key())
        assert no_note.status_code == 422
        decision_key = key()
        approved = await client.post(f"{path}/approve", json={}, headers=decision_key)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        again = await client.post(f"{path}/approve", json={}, headers=decision_key)
        assert again.status_code == 200
        late = await client.post(f"{path}/reject", json={"note": "Test"}, headers=key())
        assert late.status_code == 409
        assert late.json()["code"] == "leave.request.already_decided"
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        mine = await balance(client, w)
        assert (mine["balance"], mine["reserved"], mine["available"]) == ("3.00", "0.00", "3.00")
        history = (await client.get("/api/v1/me/leave/ledger")).json()["items"]
        assert [(e["entry_type"], e["amount"]) for e in history] == [("usage", "-2.00"), ("accrual", "5.00")]

    # The worker sends the emails: what happened and a link, never the reason.
    await api.deliver()
    [to_manager] = [m for m in api.mail_to(w.manager.email) if "leave" in m.subject]
    assert to_manager.subject.startswith("A leave request needs your decision")
    assert "/leave/approvals" in to_manager.body
    [to_employee] = [m for m in api.mail_to(w.employee.email) if "leave" in m.subject]
    assert to_employee.subject.startswith("Your leave was approved")
    for message in (to_manager, to_employee):
        assert "Test family event" not in message.body

    reads = await api.fetch(
        "SELECT count(*) FROM audit.audit_log WHERE action = 'leave.request.reason_read' AND target_id = :id",
        id=request["id"],
    )
    assert reads == [(1,)]
    decided = await api.fetch(
        "SELECT action, changes::text FROM audit.audit_log "
        "WHERE target_id = :id AND action = 'leave.request.approved'",
        id=request["id"],
    )
    assert len(decided) == 1


async def test_rejection_cancellation_and_reversal(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        rejected_one = (
            await client.post("/api/v1/me/leave-requests", json=draft(w, 1), headers=key())
        ).json()
        withdrawn = (
            await client.post(
                "/api/v1/me/leave-requests",
                json={
                    **draft(w, 1),
                    "start_date": (w.start + timedelta(days=3)).isoformat(),
                    "end_date": (w.start + timedelta(days=3)).isoformat(),
                },
                headers=key(),
            )
        ).json()
        cancelled = await client.post(f"/api/v1/me/leave-requests/{withdrawn['id']}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        approved_one = (
            await client.post(
                "/api/v1/me/leave-requests",
                json={
                    **draft(w, 1),
                    "start_date": (w.start + timedelta(days=6)).isoformat(),
                    "end_date": (w.start + timedelta(days=6)).isoformat(),
                },
                headers=key(),
            )
        ).json()
    async with api.client() as client:
        await api.sign_in(client, w.manager)
        no = await client.post(
            f"/api/v1/leave-requests/{rejected_one['id']}/reject",
            json={"note": "Test: team offsite"},
            headers=key(),
        )
        assert no.json()["status"] == "rejected"
        yes = await client.post(
            f"/api/v1/leave-requests/{approved_one['id']}/approve", json={}, headers=key()
        )
        assert yes.json()["status"] == "approved"
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        seen = (await client.get(f"/api/v1/leave-requests/{rejected_one['id']}")).json()
        assert seen["decisions"][0]["note"] == "Test: team offsite"
        assert (await balance(client, w))["balance"] == "4.00"
        asked = await client.post(f"/api/v1/me/leave-requests/{approved_one['id']}/cancel")
        assert asked.json()["status"] == "cancellation_requested"
        assert (
            await client.post(f"/api/v1/me/leave-requests/{rejected_one['id']}/cancel")
        ).status_code == 409
    async with api.client() as client:
        await api.sign_in(client, w.manager)
        done = await client.post(
            f"/api/v1/leave-requests/{approved_one['id']}/approve", json={}, headers=key()
        )
        assert done.json()["status"] == "cancelled"
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        assert (await balance(client, w))["balance"] == "5.00"
        mine = (await client.get("/api/v1/me/leave-requests")).json()["items"]
        assert {item["status"] for item in mine} == {"cancelled", "rejected"}


async def test_a_type_without_approval_is_recorded_at_once(api: Api) -> None:
    w = await world(api, type_overrides={"requires_approval": False})
    await allocate(api, w)
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        made = await client.post("/api/v1/me/leave-requests", json=draft(w, 2), headers=key())
        assert made.json()["status"] == "approved"
        assert (await balance(client, w))["balance"] == "3.00"
        cancelled = await client.post(f"/api/v1/me/leave-requests/{made.json()['id']}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        assert (await balance(client, w))["balance"] == "5.00"


async def test_the_balance_limit_and_negative_policy(api: Api) -> None:
    strict = await world(api)
    await allocate(api, strict)
    async with api.client() as client:
        await api.sign_in(client, strict.employee)
        too_much = await client.post("/api/v1/me/leave-requests", json=draft(strict, 6), headers=key())
        assert too_much.status_code == 409
        assert too_much.json()["code"] == "leave.insufficient_balance"
        preview = await client.post("/api/v1/me/leave-requests/preview", json=draft(strict, 6))
        assert preview.json()["within_balance"] is False

    lenient = await world(api, allow_negative_balance=True, max_negative_days="2")
    await allocate(api, lenient)
    async with api.client() as client:
        await api.sign_in(client, lenient.employee)
        assert (
            await client.post("/api/v1/me/leave-requests", json=draft(lenient, 7), headers=key())
        ).status_code == 201
        later = {
            **draft(lenient, 1),
            "start_date": (lenient.start + timedelta(days=10)).isoformat(),
            "end_date": (lenient.start + timedelta(days=10)).isoformat(),
        }
        beyond = await client.post("/api/v1/me/leave-requests", json=later, headers=key())
        assert beyond.status_code == 409


async def test_holidays_half_days_and_eligibility(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    holiday = w.start + timedelta(days=1)
    async with api.client() as client:
        await api.sign_in(client, w.admin)
        calendar = (
            await client.post(
                "/api/v1/holiday-calendars", json={"name": fake_code("CAL"), "year": holiday.year}
            )
        ).json()
        await client.post(
            f"/api/v1/holiday-calendars/{calendar['id']}/holidays",
            json={"date": holiday.isoformat(), "name": "Test holiday"},
            headers={"If-Match": '"1"'},
        )
        assigned = await client.put(
            f"/api/v1/holiday-calendars/{calendar['id']}/locations/{w.units.location_id}",
            headers={"If-Match": '"2"'},
        )
        assert assigned.status_code == 200, assigned.text
        other_type = (await client.post("/api/v1/leave-types", json=type_body())).json()
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        preview = (
            await client.post("/api/v1/me/leave-requests/preview", json=draft(w, 3, start_half="second"))
        ).json()
        assert [(d["counted"], d["not_counted_reason"], d["portion"]) for d in preview["days"]] == [
            (True, None, "0.50"),
            (False, "holiday", "1.00"),
            (True, None, "1.00"),
        ]
        assert preview["days_requested"] == "1.50"
        only_holiday = {**draft(w, 1), "start_date": holiday.isoformat(), "end_date": holiday.isoformat()}
        none = await client.post("/api/v1/me/leave-requests/preview", json=only_holiday)
        assert none.json()["errors"][0]["code"] == "leave.no_days"
        no_policy = await client.post(
            "/api/v1/me/leave-requests/preview", json={**draft(w, 1), "leave_type_id": other_type["id"]}
        )
        assert no_policy.json()["errors"][0]["code"] == "leave.not_eligible"
        before_joining = await client.post(
            "/api/v1/me/leave-requests/preview",
            json={**draft(w, 1), "start_date": "2000-01-03", "end_date": "2000-01-03"},
        )
        assert before_joining.json()["errors"][0]["code"] == "leave.not_employed"


# --- scope, separation of duties and concurrency --------------------------------------------------


async def test_reading_follows_scope_and_never_shows_reasons_in_lists(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        request = (
            await client.post(
                "/api/v1/me/leave-requests", json={**draft(w, 1), "reason": "Test private"}, headers=key()
            )
        ).json()
    window = {"from": w.start.isoformat(), "to": (w.start + timedelta(days=5)).isoformat()}
    async with api.client() as client:
        await api.sign_in(client, w.manager)
        calendar = (await client.get("/api/v1/leave/calendar", params=window)).json()["items"]
        assert [c["request_id"] for c in calendar] == [request["id"]]
        assert "reason" not in calendar[0]
        team = await client.get(f"/api/v1/employees/{w.employee.employee_id}/leave/balances")
        assert team.status_code == 200
    async with api.client() as client:
        await api.sign_in(client, w.stranger_manager)
        assert (await client.get("/api/v1/leave/calendar", params=window)).json()["items"] == []
        assert (
            await client.get(f"/api/v1/employees/{w.employee.employee_id}/leave/balances")
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/employees/{w.employee.employee_id}/leave/ledger")
        ).status_code == 404
    payroll = await api.create_account(roles=(RoleKey.PAYROLL_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, payroll)
        seen = (await client.get(f"/api/v1/leave-requests/{request['id']}")).json()
        # Reading all leave is not deciding it: no reason, no decision.
        assert (seen["reason"], seen["reason_visible"], seen["can_decide"]) == (None, False, False)
    for role in (RoleKey.SUPER_ADMIN, RoleKey.SYSTEM_ADMIN, RoleKey.AUDITOR):
        account = await api.create_account(roles=(role.value,))
        async with api.client() as client:
            await api.sign_in(client, account)
            assert (await client.get(f"/api/v1/leave-requests/{request['id']}")).status_code == 403
            assert (await client.get("/api/v1/leave/calendar", params=window)).status_code == 403


async def test_nobody_decides_their_own_request(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    # An HR person who is also an employee asks for leave and tries to approve it.
    hr_employee = await another_employee(api, w, roles=(RoleKey.HR.value,))
    await allocate(api, w)
    async with api.client() as client:
        await api.sign_in(client, hr_employee)
        own = (await client.post("/api/v1/me/leave-requests", json=draft(w, 1), headers=key())).json()
        refused = await client.post(f"/api/v1/leave-requests/{own['id']}/approve", json={}, headers=key())
        assert refused.status_code == 403
        inbox = (await client.get("/api/v1/leave-requests", params={"awaiting_my_decision": "true"})).json()
        assert own["id"] not in {item["id"] for item in inbox["items"]}
    async with api.client() as client:
        await api.sign_in(client, w.hr)
        assert (
            await client.post(f"/api/v1/leave-requests/{own['id']}/approve", json={}, headers=key())
        ).status_code == 200


async def test_two_approvers_deciding_at_once_leave_one_decision(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        request = (await client.post("/api/v1/me/leave-requests", json=draft(w, 1), headers=key())).json()
    async with api.client() as a, api.client() as b:
        await api.sign_in(a, w.manager)
        await api.sign_in(b, w.hr)
        responses = await asyncio.gather(
            a.post(f"/api/v1/leave-requests/{request['id']}/approve", json={}, headers=key()),
            b.post(f"/api/v1/leave-requests/{request['id']}/reject", json={"note": "Test"}, headers=key()),
        )
    assert sorted(r.status_code for r in responses) == [200, 409]
    decisions = await api.fetch(
        "SELECT count(*) FROM leave.leave_decisions WHERE leave_request_id = :id", id=request["id"]
    )
    assert decisions == [(1,)]
    usage = await api.fetch(
        "SELECT count(*) FROM leave.leave_ledger WHERE leave_request_id = :id", id=request["id"]
    )
    assert usage[0][0] in (0, 1)


async def test_concurrent_requests_cannot_overdraw_the_balance(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    async with api.client() as a, api.client() as b:
        await api.sign_in(a, w.employee)
        await api.sign_in(b, w.employee)
        second = {
            **draft(w, 3),
            "start_date": (w.start + timedelta(days=10)).isoformat(),
            "end_date": (w.start + timedelta(days=12)).isoformat(),
        }
        responses = await asyncio.gather(
            a.post("/api/v1/me/leave-requests", json=draft(w, 3), headers=key()),
            b.post("/api/v1/me/leave-requests", json=second, headers=key()),
        )
    assert sorted(r.status_code for r in responses) == [201, 409]
