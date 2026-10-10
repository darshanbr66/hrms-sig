"""Clocking in and out over HTTP (docs/product-requirements.md §6.4, api-architecture.md §8).

All data is obviously fake (docs/database-design.md §10).
"""

import secrets
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.people_data import add_job, create_employee, create_org_units


async def employee(api: Api, *, manager: uuid.UUID | None = None) -> Account:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(
            session, units, employee_id, manager_id=manager, effective_from=today - timedelta(days=30)
        )
    return await api.create_account(employee_id=employee_id)


async def clock(client: httpx.AsyncClient, event_type: str, key: str | None = None) -> httpx.Response:
    return await client.post(
        "/api/v1/me/attendance/events",
        json={"event_type": event_type},
        headers={"Idempotency-Key": key or secrets.token_urlsafe(12)},
    )


async def test_a_working_day_with_a_break(api: Api) -> None:
    account = await employee(api)
    async with api.client() as client:
        await api.sign_in(client, account)
        start = (await client.get("/api/v1/me/attendance/today")).json()
        assert start["state"] == "clocked_out"
        assert start["allowed_events"] == ["CLOCK_IN"]
        assert start["time_zone"] == "Asia/Kolkata"

        clocked_in = await clock(client, "CLOCK_IN")
        assert clocked_in.status_code == 200, clocked_in.text
        assert clocked_in.json()["state"] == "clocked_in"
        assert clocked_in.json()["allowed_events"] == ["BREAK_START", "CLOCK_OUT"]

        # Sessions time out while the clock moves on, so each step signs in again.
        api.clock.advance(timedelta(hours=2))
        await api.sign_in(client, account)
        assert (await clock(client, "BREAK_START")).json()["state"] == "on_break"
        api.clock.advance(timedelta(minutes=30))
        await api.sign_in(client, account)
        assert (await clock(client, "BREAK_END")).json()["state"] == "clocked_in"
        api.clock.advance(timedelta(hours=1))
        await api.sign_in(client, account)
        done = (await clock(client, "CLOCK_OUT")).json()
        assert done["state"] == "clocked_out"
        # The session keeps the work date it started on, even if it ended after local midnight.
        work_date = clocked_in.json()["work_date"]
        history = await client.get("/api/v1/me/attendance/days", params={"from": work_date, "to": work_date})
        [day] = history.json()["items"]
        events = day["events"]
        assert [event["event_type"] for event in events] == [
            "CLOCK_IN",
            "BREAK_START",
            "BREAK_END",
            "CLOCK_OUT",
        ]
        moments = [datetime.fromisoformat(event["occurred_at"]) for event in events]
        assert day["session_seconds"] == int((moments[3] - moments[0]).total_seconds())
        assert day["break_seconds"] == int((moments[2] - moments[1]).total_seconds())
        assert day["session_seconds"] > 3 * 3600
        assert not day["open_session"]


async def test_the_sequence_is_enforced(api: Api) -> None:
    account = await employee(api)
    async with api.client() as client:
        await api.sign_in(client, account)
        refused = await clock(client, "BREAK_START")
        assert refused.status_code == 409
        assert refused.json()["code"] == "attendance.not_clocked_in"
        assert (await clock(client, "CLOCK_IN")).status_code == 200
        twice = await clock(client, "CLOCK_IN")
        assert twice.status_code == 409
        assert twice.json()["code"] == "attendance.already_clocked_in"


async def test_a_repeated_request_is_recorded_once(api: Api) -> None:
    account = await employee(api)
    async with api.client() as client:
        await api.sign_in(client, account)
        first = await clock(client, "CLOCK_IN", key="repeat-key-1")
        second = await clock(client, "CLOCK_IN", key="repeat-key-1")
        assert first.status_code == second.status_code == 200
        assert len(second.json()["day"]["events"]) == 1
        reused = await clock(client, "CLOCK_OUT", key="repeat-key-1")
        assert reused.status_code == 422
        missing = await client.post("/api/v1/me/attendance/events", json={"event_type": "CLOCK_OUT"})
        assert missing.status_code == 422


async def test_a_session_crossing_midnight_stays_on_its_day_and_a_forgotten_clock_out_is_shown(
    api: Api,
) -> None:
    account = await employee(api)
    async with api.client() as client:
        await api.sign_in(client, account)
        clocked_in = (await clock(client, "CLOCK_IN")).json()
        api.clock.advance(timedelta(days=1, hours=2))
        await api.sign_in(client, account)
        today = (await client.get("/api/v1/me/attendance/today")).json()
        assert today["state"] == "clocked_in"
        assert today["work_date"] == clocked_in["work_date"]
        assert today["open_since"] == clocked_in["work_date"]
        closed = (await clock(client, "CLOCK_OUT")).json()
        assert closed["state"] == "clocked_out"
        assert closed["open_since"] is None
        work_date = clocked_in["work_date"]
        history = await client.get("/api/v1/me/attendance/days", params={"from": work_date, "to": work_date})
        [day] = history.json()["items"]
        assert [event["event_type"] for event in day["events"]] == ["CLOCK_IN", "CLOCK_OUT"]


async def test_events_cannot_be_changed_or_deleted(api: Api) -> None:
    account = await employee(api)
    async with api.client() as client:
        await api.sign_in(client, account)
        await clock(client, "CLOCK_IN")
    for statement in (
        "UPDATE attendance.attendance_events SET occurred_at = now() WHERE employee_id = :id",
        "DELETE FROM attendance.attendance_events WHERE employee_id = :id",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            await api.execute(statement, id=account.employee_id)


async def test_managers_see_their_team_today_and_nobody_else(api: Api) -> None:
    manager = await employee(api)
    report = await employee(api, manager=manager.employee_id)
    stranger = await employee(api)
    async with api.client() as client:
        await api.sign_in(client, report)
        await clock(client, "CLOCK_IN")
    async with api.client() as client:
        await api.sign_in(client, manager)
        team = (await client.get("/api/v1/attendance/today")).json()
        members = {item["employee"]["id"]: item for item in team["items"]}
        assert set(members) == {str(manager.employee_id), str(report.employee_id)}
        assert members[str(report.employee_id)]["state"] == "clocked_in"
        assert team["summary"] == {"in_scope": 2, "clocked_in_today": 1, "working_now": 1, "on_break": 0}
        work_date = api.clock.now().date().isoformat()
        params = {"from": (api.clock.now().date() - timedelta(days=1)).isoformat(), "to": work_date}
        own_team = await client.get(f"/api/v1/employees/{report.employee_id}/attendance/days", params=params)
        assert own_team.status_code == 200
        other = await client.get(f"/api/v1/employees/{stranger.employee_id}/attendance/days", params=params)
        assert other.status_code == 404


async def test_hr_sees_everyone_and_an_account_without_a_job_cannot_clock_in(api: Api) -> None:
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        no_job = await create_employee(session, joined=today - timedelta(days=3))
    unplaced = await api.create_account(employee_id=no_job)
    async with api.client() as client:
        await api.sign_in(client, unplaced)
        refused = await clock(client, "CLOCK_IN")
        assert refused.status_code == 409
        assert refused.json()["code"] == "attendance.no_current_job"
    async with api.client() as client:
        await api.sign_in(client, hr)
        everyone = await client.get("/api/v1/attendance/today", params={"limit": 5})
        assert everyone.status_code == 200
        assert everyone.json()["summary"]["in_scope"] >= 1
        too_long = await client.get(
            f"/api/v1/employees/{no_job}/attendance/days",
            params={"from": "2026-01-01", "to": "2026-12-31"},
        )
        assert too_long.status_code == 422
