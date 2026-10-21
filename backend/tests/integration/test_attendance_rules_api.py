"""Shifts, attendance policies, judged days, corrections and locks over HTTP
(docs/product-requirements.md ATT-4 to ATT-12, authorization-model §3.3, SOD-1, SOD-7).

Every shift time and policy value is an obviously fake test value (docs/database-design.md §10).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from pymongo.errors import OperationFailure

from app.modules.attendance.models import AttendanceEventVoid, AttendancePolicy
from app.platform.authz.roles import RoleKey
from app.platform.db import AppendOnlyError, GuardedUpdateError
from tests.api_support import Account, Api
from tests.people_data import OrgUnits, add_job, create_employee, create_org_units, fake_code

ZONE = ZoneInfo("Asia/Kolkata")


@dataclass
class Team:
    units: OrgUnits
    admin: Account
    hr: Account
    manager: Account
    employee: Account
    outsider: Account


def key() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid.uuid4()}"}


def local_today(api: Api) -> date:
    return api.clock.now().astimezone(ZONE).date()


def at(day: date, hour: int, minute: int = 0) -> str:
    return datetime.combine(day, time(hour, minute), ZONE).isoformat()


async def team(api: Api) -> Team:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        manager_id = await create_employee(session, joined=today - timedelta(days=90))
        await add_job(session, units, manager_id, manager_id=None, effective_from=today - timedelta(days=90))
        employee_id = await create_employee(session, joined=today - timedelta(days=60))
        await add_job(
            session, units, employee_id, manager_id=manager_id, effective_from=today - timedelta(days=60)
        )
        outsider_id = await create_employee(session, joined=today - timedelta(days=60))
        await add_job(session, units, outsider_id, manager_id=None, effective_from=today - timedelta(days=60))
    return Team(
        units=units,
        admin=await api.create_account(roles=(RoleKey.HR_ADMIN.value,)),
        hr=await api.create_account(roles=(RoleKey.HR.value,)),
        manager=await api.create_account(employee_id=manager_id),
        employee=await api.create_account(employee_id=employee_id),
        outsider=await api.create_account(employee_id=outsider_id),
    )


def policy_body(location_id: uuid.UUID, effective_from: date, **overrides: Any) -> dict[str, Any]:
    return {
        "scope_type": "location",
        "scope_id": str(location_id),
        "effective_from": effective_from.isoformat(),
        "grace_minutes": 7,
        "early_departure_grace_minutes": 3,
        "duration_basis": "effective",
        "full_day_min_minutes": 400,
        "half_day_min_minutes": 200,
        "max_break_minutes": None,
        "breaks_count_as_work": False,
        "break_excess_action": "flag",
        "overtime_enabled": False,
        "overtime_requires_approval": False,
        "incomplete_after_minutes": 60,
        "allow_multiple_sessions": True,
        **overrides,
    }


async def configure(api: Api, t: Team, **policy: Any) -> str:
    """A test shift (09:00 to 17:30, Sundays off) as the location default, and a location
    policy, both from 60 days ago. Returns the shift ID."""
    start = api.clock.now().date() - timedelta(days=60)
    async with api.client() as client:
        await api.sign_in(client, t.admin)
        shift = await client.post(
            "/api/v1/shifts",
            json={
                "code": fake_code("SH"),
                "name": "Test day shift",
                "version": {
                    "effective_from": start.isoformat(),
                    "start_time": "09:00",
                    "end_time": "17:30",
                    "weekly_offs": [{"weekday": 7}],
                },
            },
        )
        assert shift.status_code == 201, shift.text
        shift_id = shift.json()["id"]
        default = await client.post(
            "/api/v1/location-default-shifts",
            json={
                "location_id": str(t.units.location_id),
                "shift_id": shift_id,
                "effective_from": start.isoformat(),
            },
        )
        assert default.status_code == 201, default.text
        created = await client.post(
            "/api/v1/attendance-policies", json=policy_body(t.units.location_id, start, **policy)
        )
        assert created.status_code == 201, created.text
    return str(shift_id)


def a_weekday_before(day: date, days_back: int) -> date:
    candidate = day - timedelta(days=days_back)
    while candidate.isoweekday() == 7:
        candidate -= timedelta(days=1)
    return candidate


async def days(client: httpx.AsyncClient, path: str, day: date) -> dict[str, Any]:
    response = await client.get(path, params={"from": day.isoformat(), "to": day.isoformat()})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    return items[0] if items else {}


# --- configuration -------------------------------------------------------------------------------


async def test_shifts_and_policies_are_configured_by_hr_admins_only(api: Api) -> None:
    t = await team(api)
    async with api.client() as client:
        await api.sign_in(client, t.hr)
        refused = await client.post(
            "/api/v1/attendance-policies", json=policy_body(t.units.location_id, date(2030, 1, 1))
        )
        assert refused.status_code == 403
    async with api.client() as client:
        await api.sign_in(client, t.admin)
        half_above_full = await client.post(
            "/api/v1/attendance-policies",
            json=policy_body(t.units.location_id, date(2030, 1, 1), half_day_min_minutes=500),
        )
        assert half_above_full.json()["errors"][0]["code"] == "attendance.half_day_above_full_day"
        overtime_without_basis = await client.post(
            "/api/v1/attendance-policies",
            json=policy_body(t.units.location_id, date(2030, 1, 1), overtime_enabled=True),
        )
        assert overtime_without_basis.json()["errors"][0]["code"] == "attendance.overtime_incomplete"
        first = await client.post(
            "/api/v1/attendance-policies", json=policy_body(t.units.location_id, date(2030, 1, 1))
        )
        assert first.status_code == 201
        later = await client.post(
            "/api/v1/attendance-policies",
            json=policy_body(t.units.location_id, date(2030, 6, 1), grace_minutes=11),
        )
        assert later.status_code == 201
        earlier = await client.post(
            "/api/v1/attendance-policies", json=policy_body(t.units.location_id, date(2030, 3, 1))
        )
        assert earlier.status_code == 409
        assert earlier.json()["code"] == "attendance.version_overlap"
        listed = [
            p
            for p in (await client.get("/api/v1/attendance-policies")).json()["items"]
            if p["scope_id"] == str(t.units.location_id)
        ]
        assert {(p["effective_from"], p["effective_to"]) for p in listed} == {
            ("2030-01-01", "2030-06-01"),
            ("2030-06-01", None),
        }
    # Versions are never edited beyond closing them.
    with pytest.raises(GuardedUpdateError):  # noqa: PT012 (the commit raises)
        async with api.database.unit_of_work() as session:
            policy = await session.get(AttendancePolicy, uuid.UUID(first.json()["id"]))
            assert policy is not None
            policy.grace_minutes = 0 if policy.grace_minutes else 1


async def test_setup_lists_who_has_no_shift_or_policy(api: Api) -> None:
    t = await team(api)
    async with api.client() as client:
        await api.sign_in(client, t.hr)
        before = (await client.get("/api/v1/attendance/setup")).json()
        assert str(t.employee.employee_id) in {p["id"] for p in before["without_shift"]}
    await configure(api, t)
    async with api.client() as client:
        await api.sign_in(client, t.hr)
        after = (await client.get("/api/v1/attendance/setup")).json()
        missing = {p["id"] for p in after["without_shift"]} | {p["id"] for p in after["without_policy"]}
        assert str(t.employee.employee_id) not in missing
    async with api.client() as client:
        await api.sign_in(client, t.manager)
        team_view = (await client.get("/api/v1/attendance/setup")).json()
        # Their own attendance and their team's.
        assert team_view["employees_in_scope"] == 2


# --- judged days and corrections ----------------------------------------------------------------


async def test_days_are_judged_and_a_correction_is_approved_without_losing_the_original(  # noqa: PLR0915 (one workflow end to end)
    api: Api,
) -> None:
    t = await team(api)
    await configure(api, t)
    today = local_today(api)
    day = a_weekday_before(today, 3)
    sunday = today - timedelta(days=(today.isoweekday() % 7) or 7)
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        absent = await days(client, "/api/v1/me/attendance/days", day)
        assert absent["status"] == "absent"
        assert absent["judged"] is True
        assert (await days(client, "/api/v1/me/attendance/days", sunday))["status"] == "weekly_off"

        no_reason = await client.post(
            "/api/v1/me/attendance/corrections",
            json={
                "work_date": day.isoformat(),
                "reason": "",
                "items": [{"action": "add", "event_type": "CLOCK_IN", "proposed_at": at(day, 9, 20)}],
            },
            headers=key(),
        )
        assert no_reason.status_code == 422
        submitted = await client.post(
            "/api/v1/me/attendance/corrections",
            json={
                "work_date": day.isoformat(),
                "reason": "Test: the clock-in device was down",
                "items": [
                    {"action": "add", "event_type": "CLOCK_IN", "proposed_at": at(day, 9, 20)},
                    {"action": "add", "event_type": "CLOCK_OUT", "proposed_at": at(day, 18, 0)},
                ],
            },
            headers=key(),
        )
        assert submitted.status_code == 201, submitted.text
        correction = submitted.json()
        assert correction["correction"]["status"] == "pending"
        assert correction["reason"] == "Test: the clock-in device was down"
        assert (await days(client, "/api/v1/me/attendance/days", day))["status"] == "pending_correction"
        twice = await client.post(
            "/api/v1/me/attendance/corrections",
            json={
                "work_date": day.isoformat(),
                "reason": "Again",
                "items": [{"action": "add", "event_type": "CLOCK_IN", "proposed_at": at(day, 9)}],
            },
            headers=key(),
        )
        assert twice.status_code == 409
        assert twice.json()["code"] == "correction.pending_exists"

    correction_id = correction["correction"]["id"]
    async with api.client() as client:
        await api.sign_in(client, t.hr)
        listed = (
            await client.get(
                "/api/v1/attendance/corrections", params={"employee_id": str(t.employee.employee_id)}
            )
        ).json()
        assert [c["id"] for c in listed["items"]] == [correction_id]
        assert "reason" not in listed["items"][0]
    async with api.client() as client:
        await api.sign_in(client, t.outsider)
        assert (await client.get(f"/api/v1/attendance/corrections/{correction_id}")).status_code == 404
    async with api.client() as client:
        await api.sign_in(client, t.manager)
        inbox = (await client.get("/api/v1/attendance/corrections", params={"for_decision": "true"})).json()
        assert correction_id in [c["id"] for c in inbox["items"]]
        notes = (await client.get("/api/v1/me/notifications")).json()
        assert any(n["type"] == "attendance.correction_requested" for n in notes["items"])
        detail = (await client.get(f"/api/v1/attendance/corrections/{correction_id}")).json()
        assert detail["reason"] == "Test: the clock-in device was down"
        assert detail["before"] == []
        assert [e["event_type"] for e in detail["after"]] == ["CLOCK_IN", "CLOCK_OUT"]
        assert detail["can_decide"] is True
        silent = await client.post(f"/api/v1/attendance/corrections/{correction_id}/reject", json={})
        assert silent.json()["errors"][0]["code"] == "correction.note_required"
        approved = await client.post(f"/api/v1/attendance/corrections/{correction_id}/approve", json={})
        assert approved.status_code == 200, approved.text
        assert approved.json()["correction"]["status"] == "approved"
        again = await client.post(f"/api/v1/attendance/corrections/{correction_id}/approve", json={})
        assert again.json()["code"] == "correction.already_decided"
    reason_reads = await api.count(
        "audit_log", {"action": "attendance.correction.reason_read", "target_id": str(correction_id)}
    )
    assert reason_reads >= 1

    async with api.client() as client:
        await api.sign_in(client, t.employee)
        judged = await days(client, "/api/v1/me/attendance/days", day)
        assert judged["status"] == "present"
        assert judged["late_minutes"] == 13
        assert [e["source"] for e in judged["events"]] == ["correction", "correction"]
        clock_out = next(e for e in judged["events"] if e["event_type"] == "CLOCK_OUT")
        # A second correction replaces the clock-out; the original stays, voided.
        replaced = await client.post(
            "/api/v1/me/attendance/corrections",
            json={
                "work_date": day.isoformat(),
                "reason": "Test: I left later",
                "items": [
                    {"action": "void", "target_event_id": clock_out["id"]},
                    {"action": "add", "event_type": "CLOCK_OUT", "proposed_at": at(day, 19, 0)},
                ],
            },
            headers=key(),
        )
        assert replaced.status_code == 201, replaced.text
    async with api.client() as client:
        await api.sign_in(client, t.manager)
        assert (
            await client.post(
                f"/api/v1/attendance/corrections/{replaced.json()['correction']['id']}/approve", json={}
            )
        ).status_code == 200
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        final = await days(client, "/api/v1/me/attendance/days", day)
        assert [(e["event_type"], e["voided"]) for e in final["events"]] == [
            ("CLOCK_IN", False),
            ("CLOCK_OUT", True),
            ("CLOCK_OUT", False),
        ]
    assert await api.count("attendance_events", {"employee_id": t.employee.employee_id}) == 3
    # A void is append-only for the application and for the API's database user.
    with pytest.raises(AppendOnlyError):  # noqa: PT012 (the delete raises)
        async with api.database.unit_of_work() as session:
            [void] = await session.find(AttendanceEventVoid, {"event_id": uuid.UUID(clock_out["id"])})
            await session.delete(void)
    with pytest.raises(OperationFailure, match="not authorized"):
        await api.database.collection("attendance_event_voids").delete_many(
            {"organization_id": api.organization_id, "event_id": uuid.UUID(clock_out["id"])}
        )


async def test_nobody_decides_their_own_correction(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    day = a_weekday_before(local_today(api), 2)
    hr_account = await api.create_account(roles=(RoleKey.HR.value,), employee_id=None)
    async with api.database.unit_of_work() as session:
        today = api.clock.now().date()
        hr_employee_id = await create_employee(session, joined=today - timedelta(days=60))
        await add_job(
            session, t.units, hr_employee_id, manager_id=None, effective_from=today - timedelta(days=60)
        )
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        created = await client.post(
            "/api/v1/me/attendance/corrections",
            json={
                "work_date": day.isoformat(),
                "reason": "Test",
                "items": [
                    {"action": "add", "event_type": "CLOCK_IN", "proposed_at": at(day, 9)},
                    {"action": "add", "event_type": "CLOCK_OUT", "proposed_at": at(day, 17)},
                ],
            },
            headers=key(),
        )
        correction_id = created.json()["correction"]["id"]
        own = await client.post(f"/api/v1/attendance/corrections/{correction_id}/approve", json={})
        assert own.status_code == 403
    async with api.client() as client:
        await api.sign_in(client, t.outsider)
        assert (
            await client.post(f"/api/v1/attendance/corrections/{correction_id}/approve", json={})
        ).status_code == 403
    # SOD-7: HR records corrections for others at once, never for themselves.
    async with api.client() as client:
        await api.sign_in(client, hr_account)
        other_day = a_weekday_before(local_today(api), 5)
        recorded = await client.post(
            f"/api/v1/employees/{t.outsider.employee_id}/attendance/corrections",
            json={
                "work_date": other_day.isoformat(),
                "reason": "Test: recorded by HR",
                "items": [
                    {"action": "add", "event_type": "CLOCK_IN", "proposed_at": at(other_day, 9)},
                    {"action": "add", "event_type": "CLOCK_OUT", "proposed_at": at(other_day, 17)},
                ],
            },
            headers=key(),
        )
        assert recorded.status_code == 201, recorded.text
        assert recorded.json()["correction"]["kind"] == "hr_recorded"
        assert recorded.json()["correction"]["status"] == "approved"
    hr_with_record = await api.create_account(roles=(RoleKey.HR.value,), employee_id=hr_employee_id)
    async with api.client() as client:
        await api.sign_in(client, hr_with_record)
        own_day = a_weekday_before(local_today(api), 4)
        own = await client.post(
            f"/api/v1/employees/{hr_employee_id}/attendance/corrections",
            json={
                "work_date": own_day.isoformat(),
                "reason": "Test",
                "items": [{"action": "add", "event_type": "CLOCK_IN", "proposed_at": at(own_day, 9)}],
            },
            headers=key(),
        )
        assert own.status_code == 403
        assert own.json()["code"] == "sod.own_request"


async def test_a_locked_date_needs_hr_with_the_all_scope(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    day = a_weekday_before(local_today(api), 10)
    async with api.client() as client:
        await api.sign_in(client, t.admin)
        lock = await client.post(
            "/api/v1/attendance/locks",
            json={
                "period_start": (day - timedelta(days=1)).isoformat(),
                "period_end": day.isoformat(),
                "reason": "Test: period closed",
            },
        )
        assert lock.status_code == 201, lock.text
    try:
        async with api.client() as client:
            await api.sign_in(client, t.employee)
            created = await client.post(
                "/api/v1/me/attendance/corrections",
                json={
                    "work_date": day.isoformat(),
                    "reason": "Test",
                    "items": [
                        {"action": "add", "event_type": "CLOCK_IN", "proposed_at": at(day, 9)},
                        {"action": "add", "event_type": "CLOCK_OUT", "proposed_at": at(day, 17)},
                    ],
                },
                headers=key(),
            )
            assert created.json()["correction"]["locked_period"] is True
            correction_id = created.json()["correction"]["id"]
        async with api.client() as client:
            await api.sign_in(client, t.manager)
            refused = await client.post(f"/api/v1/attendance/corrections/{correction_id}/approve", json={})
            assert refused.status_code == 403
            assert refused.json()["code"] == "attendance.locked"
        async with api.client() as client:
            await api.sign_in(client, t.hr)
            assert (
                await client.post(f"/api/v1/attendance/corrections/{correction_id}/approve", json={})
            ).status_code == 200
    finally:
        async with api.client() as client:
            await api.sign_in(client, t.admin)
            released = await client.post(
                f"/api/v1/attendance/locks/{lock.json()['id']}/release", json={"reason": "Test done"}
            )
            assert released.status_code == 200


async def test_one_session_a_day_when_the_policy_says_so(api: Api) -> None:
    t = await team(api)
    await configure(api, t, allow_multiple_sessions=False)
    async with api.client() as client:
        await api.sign_in(client, t.outsider)
        assert (
            await client.post("/api/v1/me/attendance/events", json={"event_type": "CLOCK_IN"}, headers=key())
        ).status_code == 200
        assert (
            await client.post("/api/v1/me/attendance/events", json={"event_type": "CLOCK_OUT"}, headers=key())
        ).status_code == 200
        again = await client.post(
            "/api/v1/me/attendance/events", json={"event_type": "CLOCK_IN"}, headers=key()
        )
        assert again.status_code == 409
        assert again.json()["code"] == "attendance.single_session"


async def test_the_register_and_report_follow_the_readers_scope(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    day = a_weekday_before(local_today(api), 2)
    async with api.client() as client:
        await api.sign_in(client, t.manager)
        register = (await client.get("/api/v1/attendance/register", params={"date": day.isoformat()})).json()
        # A manager reads their own attendance and their team's, never anyone else's.
        mine = {str(t.manager.employee_id), str(t.employee.employee_id)}
        assert register["in_scope"] == 2
        assert {i["employee"]["id"] for i in register["items"]} == mine
        assert register["counts"] == {"absent": 2}
        report = (
            await client.get(
                "/api/v1/attendance/report",
                params={"from": (day - timedelta(days=6)).isoformat(), "to": day.isoformat()},
            )
        ).json()
        assert {r["employee"]["id"] for r in report["items"]} == mine
        assert all(r["days"].get("absent", 0) >= 5 for r in report["items"])
    async with api.client() as client:
        await api.sign_in(client, t.hr)
        filtered = (
            await client.get(
                "/api/v1/attendance/register",
                params={"date": day.isoformat(), "location_id": str(t.units.location_id), "status": "absent"},
            )
        ).json()
        assert {i["employee"]["id"] for i in filtered["items"]} == {
            str(t.employee.employee_id),
            str(t.outsider.employee_id),
            str(t.manager.employee_id),
        }
