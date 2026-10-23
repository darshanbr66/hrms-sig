"""Correcting a forgotten clock-out through the HR-recorded correction (product-requirements
ATT-6, ATT-7; authorization-model SOD-7; owner decision 2026-10-23): HR enters the verified
departure time with a reason, nothing recorded is changed or deleted, and the day, the
register and the period report are judged again from the result under the employee's shift,
policy and location time zone. A session left open never counts past its last event, so it
never reads as hours worked or overtime; a genuinely long day keeps its overtime.

Shift times and policy values are obviously fake test values (docs/database-design.md §10).
"""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx

from app.modules.attendance.models import AttendanceEvent, EventSource
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.integration.test_attendance_rules_api import (
    ZONE,
    Team,
    a_weekday_before,
    at,
    configure,
    days,
    key,
    local_today,
    policy_body,
    team,
)
from tests.people_data import add_job, create_employee, fake_code

# Overtime from 30 minutes past the shift's end (17:30), needing approval.
OVERTIME_PAST_SHIFT_END: dict[str, Any] = {
    "overtime_enabled": True,
    "overtime_basis": "beyond_shift_end",
    "overtime_min_minutes": 30,
    "overtime_requires_approval": True,
}


async def clocked(api: Api, account: Account, day: date, *events: tuple[str, int, int]) -> None:
    """Live events (as clocked from the web) on a work date, at local times."""
    employee_id = account.employee_id
    assert employee_id is not None
    async with api.database.unit_of_work() as session:
        for event_type, hour, minute in events:
            session.add(
                AttendanceEvent(
                    employee_id=employee_id,
                    event_type=event_type,
                    occurred_at=datetime.combine(day, time(hour, minute), ZONE),
                    work_date=day,
                    source=EventSource.WEB.value,
                    recorded_by=account.user_id,
                    idempotency_key=f"test-{uuid.uuid4()}",
                )
            )


async def correct(
    client: httpx.AsyncClient,
    employee: Account,
    day: date,
    *adds: tuple[str, str],
    reason: str = "Test: left at the time the gate log shows",
    idempotency: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/employees/{employee.employee_id}/attendance/corrections",
        json={
            "work_date": day.isoformat(),
            "reason": reason,
            "items": [{"action": "add", "event_type": kind, "proposed_at": moment} for kind, moment in adds],
        },
        headers=idempotency or key(),
    )


async def register_row(client: httpx.AsyncClient, day: date, employee: Account) -> dict[str, Any]:
    response = await client.get("/api/v1/attendance/register", params={"date": day.isoformat(), "limit": 100})
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["items"] if row["employee"]["id"] == str(employee.employee_id))


async def report_row(client: httpx.AsyncClient, day: date, employee: Account) -> dict[str, Any]:
    response = await client.get(
        "/api/v1/attendance/report", params={"from": day.isoformat(), "to": day.isoformat(), "limit": 100}
    )
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["items"] if row["employee"]["id"] == str(employee.employee_id))


async def today_row(client: httpx.AsyncClient, employee: Account) -> dict[str, Any]:
    response = await client.get("/api/v1/attendance/today", params={"limit": 100})
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["items"] if row["employee"]["id"] == str(employee.employee_id))


async def signed_in(api: Api, client: httpx.AsyncClient, account: Account) -> None:
    await api.sign_in(client, account)
    await api.step_up(client, account)


async def test_a_forgotten_clock_out_is_closed_at_the_verified_departure_time(api: Api) -> None:
    t = await team(api)
    await configure(api, t, **OVERTIME_PAST_SHIFT_END)
    day = a_weekday_before(local_today(api), 3)
    await clocked(api, t.employee, day, ("CLOCK_IN", 9, 5))
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        # Before: the session is open since that day, flagged, and not counted past its clock-in.
        now_row = await today_row(client, t.employee)
        assert now_row["open_since"] == day.isoformat()
        assert now_row["state"] == "clocked_in"
        assert now_row["session_seconds"] == 0
        assert now_row["time_zone"] == "Asia/Kolkata"
        before = await register_row(client, day, t.employee)
        assert before["day"]["status"] == "incomplete"
        assert before["day"]["session_seconds"] == 0
        assert (before["day"]["effective_minutes"], before["day"]["overtime_minutes"]) == (0, 0)
        assert (await report_row(client, day, t.employee))["days"].get("incomplete") == 1

        recorded = await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 17, 40)))
        assert recorded.status_code == 201, recorded.text
        detail = recorded.json()
        assert detail["correction"]["kind"] == "hr_recorded"
        assert detail["correction"]["status"] == "approved"
        assert [e["event_type"] for e in detail["before"]] == ["CLOCK_IN"]
        assert [e["event_type"] for e in detail["after"]] == ["CLOCK_IN", "CLOCK_OUT"]

        after = await register_row(client, day, t.employee)
        assert after["day"]["status"] == "present"
        assert after["day"]["effective_minutes"] == 8 * 60 + 35
        # Ten minutes past the shift's end is under the policy's 30-minute minimum: no overtime.
        assert after["day"]["overtime_minutes"] == 0
        assert [(e["event_type"], e["source"]) for e in after["day"]["events"]] == [
            ("CLOCK_IN", "web"),
            ("CLOCK_OUT", "correction"),
        ]
        report = await report_row(client, day, t.employee)
        assert report["days"].get("present") == 1
        assert "incomplete" not in report["days"]
        assert report["effective_minutes"] == 8 * 60 + 35
        assert (await today_row(client, t.employee))["open_since"] is None

    # The original clock-in is untouched; the clock-out is a new event; the change is audited
    # with the acting HR account and the reason.
    assert await api.count("attendance_events", {"employee_id": t.employee.employee_id}) == 2
    [row] = await api.records(
        AuditLogRecord, {"actor_user_id": t.hr.user_id, "action": "attendance.correction.recorded"}
    )
    assert row.subject_employee_id == t.employee.employee_id
    assert row.permission_used == "attendance.correction.create.all"
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        notes = (await client.get("/api/v1/me/notifications")).json()["items"]
        assert any(n["type"].startswith("attendance.correction") for n in notes)


async def test_a_genuinely_long_day_keeps_its_overtime(api: Api) -> None:
    t = await team(api)
    await configure(api, t, **OVERTIME_PAST_SHIFT_END)
    day = a_weekday_before(local_today(api), 3)
    await clocked(api, t.employee, day, ("CLOCK_IN", 9, 0))
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        recorded = await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 20, 0)))
        assert recorded.status_code == 201, recorded.text
        judged = (await register_row(client, day, t.employee))["day"]
        # Beyond the 17:30 shift end by 150 minutes, flagged for approval as the policy requires.
        assert judged["overtime_minutes"] == 150
        assert "overtime_needs_approval" in judged["anomalies"]
        assert (await report_row(client, day, t.employee))["overtime_minutes"] == 150


async def test_a_long_session_with_long_breaks_is_not_overtime(api: Api) -> None:
    """Ten hours between clocking in and out, two of them on a break: eight hours worked, which
    is the full day on this policy, so no overtime, whatever the length of the session."""
    t = await team(api)
    await configure(
        api,
        t,
        full_day_min_minutes=480,
        overtime_enabled=True,
        overtime_basis="beyond_full_day",
        overtime_min_minutes=30,
        overtime_requires_approval=False,
    )
    day = a_weekday_before(local_today(api), 3)
    await clocked(
        api,
        t.employee,
        day,
        ("CLOCK_IN", 9, 0),
        ("BREAK_START", 12, 0),
        ("BREAK_END", 14, 0),
        ("CLOCK_OUT", 19, 0),
    )
    async with api.client() as client:
        await api.sign_in(client, t.hr)
        judged = (await register_row(client, day, t.employee))["day"]
    assert judged["session_seconds"] == 10 * 3600
    assert judged["break_seconds"] == 2 * 3600
    assert (judged["effective_minutes"], judged["overtime_minutes"]) == (480, 0)
    assert judged["status"] == "present"


async def test_a_session_forgotten_on_a_break_is_closed_with_the_break_end(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    day = a_weekday_before(local_today(api), 3)
    earlier = a_weekday_before(day, 2)
    await clocked(api, t.employee, earlier, ("CLOCK_IN", 9, 0), ("BREAK_START", 16, 0))
    await clocked(api, t.employee, day, ("CLOCK_IN", 9, 0), ("BREAK_START", 13, 0))
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        assert (await today_row(client, t.employee))["state"] == "on_break"
        # With the verified break end, the break counts only as long as it lasted.
        closed = await correct(
            client, t.employee, day, ("BREAK_END", at(day, 13, 45)), ("CLOCK_OUT", at(day, 17, 45))
        )
        assert closed.status_code == 201, closed.text
        judged = (await register_row(client, day, t.employee))["day"]
        assert judged["break_seconds"] == 45 * 60
        assert judged["effective_minutes"] == 8 * 60
        assert judged["status"] == "present"
        # Without it (they left during the break), the break runs to the clock-out and the day
        # says the break's end was not recorded: nothing is invented.
        alone = await correct(client, t.employee, earlier, ("CLOCK_OUT", at(earlier, 17, 30)))
        assert alone.status_code == 201, alone.text
        flagged = (await register_row(client, earlier, t.employee))["day"]
    assert flagged["break_seconds"] == 90 * 60
    assert flagged["effective_minutes"] == 7 * 60
    assert "missing_break_end" in flagged["anomalies"]


async def test_invalid_and_duplicate_clock_outs_are_refused(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    today = local_today(api)
    day = a_weekday_before(today, 4)
    later = a_weekday_before(today, 2)
    await clocked(api, t.employee, day, ("CLOCK_IN", 9, 0))
    await clocked(api, t.employee, later, ("CLOCK_IN", 9, 0), ("CLOCK_OUT", 17, 0))
    async with api.client() as client:
        await signed_in(api, client, t.hr)

        async def code(*adds: tuple[str, str]) -> str:
            response = await correct(client, t.employee, day, *adds)
            assert response.status_code == 422, response.text
            return str(response.json()["errors"][0]["code"])

        assert await code(("CLOCK_OUT", at(day, 8, 0))) == "correction.sequence"  # before the clock-in
        assert await code(("CLOCK_OUT", at(day, 9, 0))) == "correction.same_time"
        assert await code(("CLOCK_OUT", at(day + timedelta(days=2), 9, 0))) == "correction.outside_day"
        future = datetime.combine(today + timedelta(days=1), time(9, 0), ZONE).isoformat()
        assert await code(("CLOCK_OUT", future)) in ("correction.in_future", "correction.outside_day")
        # Into the next recorded day.
        assert await code(("CLOCK_OUT", at(later, 10, 0))) in (
            "correction.overlaps_later",
            "correction.outside_day",
        )
        no_reason = await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 17, 30)), reason="")
        assert no_reason.status_code == 422

        # The same request sent twice records one correction.
        same = key()
        first = await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 17, 30)), idempotency=same)
        assert first.status_code == 201, first.text
        repeat = await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 17, 30)), idempotency=same)
        assert repeat.status_code in (200, 201)
        assert repeat.json()["correction"]["id"] == first.json()["correction"]["id"]
        # A second clock-out on a closed session is refused.
        assert await code(("CLOCK_OUT", at(day, 18, 0))) == "correction.sequence"
    assert await api.count("attendance_corrections", {"employee_id": t.employee.employee_id}) == 1


async def test_only_hr_with_the_all_scope_corrects_and_never_their_own_day(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    day = a_weekday_before(local_today(api), 3)
    await clocked(api, t.employee, day, ("CLOCK_IN", 9, 0))
    for actor in (t.employee, t.manager, t.outsider):
        async with api.client() as client:
            await api.sign_in(client, actor)
            refused = await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 17, 30)))
            assert refused.status_code in (403, 404), refused.text
    for role in (RoleKey.SUPER_ADMIN, RoleKey.SYSTEM_ADMIN, RoleKey.PAYROLL_ADMIN, RoleKey.AUDITOR):
        account = await api.create_account(roles=(role.value,))
        async with api.client() as client:
            await signed_in(api, client, account)
            refused = await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 17, 30)))
            assert refused.status_code in (403, 404), role
    # HR whose own account is the employee's: SOD-7, their own day goes through a request.
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        own_record = await create_employee(session, joined=today - timedelta(days=60))
        await add_job(
            session, t.units, own_record, manager_id=None, effective_from=today - timedelta(days=60)
        )
    hr_employee = await api.create_account(roles=(RoleKey.HR.value,), employee_id=own_record)
    await clocked(api, hr_employee, day, ("CLOCK_IN", 9, 0))
    async with api.client() as client:
        await signed_in(api, client, hr_employee)
        own = await correct(client, hr_employee, day, ("CLOCK_OUT", at(day, 17, 30)))
        assert own.status_code == 403
    # HR admin may, like HR.
    async with api.client() as client:
        await signed_in(api, client, t.admin)
        assert (await correct(client, t.employee, day, ("CLOCK_OUT", at(day, 17, 30)))).status_code == 201
    assert await api.count("attendance_events", {"employee_id": t.employee.employee_id}) == 2


async def test_a_night_shift_clock_out_falls_on_the_next_calendar_day(api: Api) -> None:
    """A shift crossing midnight (22:00 to 06:00): the work date is the day it starts, the
    clock-out the next morning in the location's time zone, and no overtime is invented."""
    t: Team = await team(api)
    start = api.clock.now().date() - timedelta(days=60)
    async with api.client() as client:
        await api.sign_in(client, t.admin)
        shift = await client.post(
            "/api/v1/shifts",
            json={
                "code": fake_code("SH"),
                "name": "Test night shift",
                "version": {
                    "effective_from": start.isoformat(),
                    "start_time": "22:00",
                    "end_time": "06:00",
                    "weekly_offs": [],
                },
            },
        )
        assert shift.status_code == 201, shift.text
        assert shift.json()["versions"][0]["crosses_midnight"] is True
        await client.post(
            "/api/v1/location-default-shifts",
            json={
                "location_id": str(t.units.location_id),
                "shift_id": shift.json()["id"],
                "effective_from": start.isoformat(),
            },
        )
        policy = await client.post(
            "/api/v1/attendance-policies",
            json=policy_body(t.units.location_id, start, **OVERTIME_PAST_SHIFT_END),
        )
        assert policy.status_code == 201, policy.text
    day = local_today(api) - timedelta(days=4)
    await clocked(api, t.employee, day, ("CLOCK_IN", 22, 0))
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        assert (await register_row(client, day, t.employee))["day"]["status"] == "incomplete"
        recorded = await correct(client, t.employee, day, ("CLOCK_OUT", at(day + timedelta(days=1), 6, 10)))
        assert recorded.status_code == 201, recorded.text
        judged = (await register_row(client, day, t.employee))["day"]
        assert judged["status"] == "present"
        assert judged["effective_minutes"] == 8 * 60 + 10
        assert judged["overtime_minutes"] == 0
        last_out = datetime.fromisoformat(judged["last_out"]).astimezone(ZONE)
        assert (last_out.date(), last_out.time()) == (day + timedelta(days=1), time(6, 10))
        member_view = await days(client, f"/api/v1/employees/{t.employee.employee_id}/attendance/days", day)
        assert member_view["status"] == "present"


async def test_forgotten_clock_outs_are_not_counted_as_working_now(isolated_api: Api) -> None:
    """The counts cover the whole scope, so this uses an organization of its own."""
    api = isolated_api
    t = await team(api)
    await configure(api, t)
    today = local_today(api)
    await clocked(api, t.employee, a_weekday_before(today, 3), ("CLOCK_IN", 9, 0))
    await clocked(api, t.outsider, a_weekday_before(today, 3), ("CLOCK_IN", 9, 0), ("BREAK_START", 12, 0))
    async with api.client() as client:
        await api.sign_in(client, t.hr)
        summary = (await client.get("/api/v1/attendance/today")).json()["summary"]
    assert (summary["working_now"], summary["on_break"]) == (0, 0)
