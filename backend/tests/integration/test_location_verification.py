"""Office location verification over HTTP (ADR-031): checks and approvals, evidence written with
the clock event and never instead of it, no coordinates stored or logged, review within scope,
append-only history, erasure and tenant isolation. Coordinates and limits are obviously fake
test values (docs/database-design.md §10)."""

import asyncio
import json
import logging
import math
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pymongo.errors import OperationFailure, WriteError

from app.modules.attendance import exports
from app.modules.attendance.erasure import erase_employee
from app.modules.attendance.models import AttendanceEvent, LocationEvidence, LocationReview
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from app.platform.db import AppendOnlyError, to_document
from tests.api_support import Account, Api, running_api
from tests.conftest import MongoServer
from tests.integration.test_attendance_rules_api import Team, team
from tests.people_data import add_job, create_employee, create_org_units

OFFICE = (12.0, 77.0)
M_PER_DEGREE = math.radians(1) * 6_371_008.8
# A latitude that appears nowhere else, to look for in stored records and logs.
SECRET_LATITUDE = 12.00012345


@pytest.fixture
async def api(migrated_mongo: MongoServer) -> AsyncIterator[Api]:
    """These tests exercise the module itself: an installation with the switch turned on
    (`LOCATION_CHECKS_ENABLED`, off by default; test_location_switch.py covers it off)."""
    async with running_api(migrated_mongo, location_checks_enabled=True) as running:
        yield running


def check_body(
    location_id: uuid.UUID, on: str, *, at: tuple[float, float] = OFFICE, **extra: Any
) -> dict[str, Any]:
    return {
        "location_id": str(location_id),
        "effective_from": on,
        "mode": "verify",
        "latitude": at[0],
        "longitude": at[1],
        "radius_m": 150,
        "max_accuracy_m": 100,
        "max_position_age_seconds": 120,
        "review_unverified": True,
        **extra,
    }


def captured(
    metres_north: float = 0, *, accuracy: float = 10, at: tuple[float, float] = OFFICE
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "status": "captured",
        "latitude": at[0] + metres_north / M_PER_DEGREE,
        "longitude": at[1],
        "accuracy_m": accuracy,
        "captured_at": (now - timedelta(seconds=3)).isoformat(),
        "sent_at": now.isoformat(),
    }


async def press(
    client: httpx.AsyncClient, event_type: str, location: dict[str, Any] | None, key: str | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"event_type": event_type}
    if location is not None:
        body["location"] = location
    return await client.post(
        "/api/v1/me/attendance/events", json=body, headers={"Idempotency-Key": key or f"test-{uuid.uuid4()}"}
    )


async def signed_in(api: Api, client: httpx.AsyncClient, account: Account) -> None:
    await api.sign_in(client, account)
    await api.step_up(client, account)


def today(api: Api) -> str:
    return api.clock.now().date().isoformat()


async def configure(api: Api, t: Team, **extra: Any) -> dict[str, Any]:
    async with api.client() as client:
        await signed_in(api, client, t.admin)
        created = await client.post(
            "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, today(api), **extra)
        )
        assert created.status_code == 201, created.text
        return dict(created.json())


async def evidence_of(api: Api, account: Account) -> list[LocationEvidence]:
    return await api.records(
        LocationEvidence, {"employee_id": account.employee_id}, sort=[("received_at", 1)]
    )


async def test_without_a_check_nothing_is_asked_or_stored(api: Api) -> None:
    t = await team(api)
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        assert (await client.get("/api/v1/me/attendance/today")).json()["location_check"] == "off"
        # A position sent anyway is ignored, and the event is recorded as always.
        done = await press(client, "CLOCK_IN", captured())
        assert done.status_code == 200, done.text
        assert done.json()["day"]["events"][0]["location"] is None
    assert await evidence_of(api, t.employee) == []


async def test_every_outcome_is_recorded_with_the_event_and_never_stops_it(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    t = await team(api)
    await configure(api, t)
    caplog.set_level(logging.DEBUG)
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        assert (await client.get("/api/v1/me/attendance/today")).json()["location_check"] == "requested"
        at_office = {**captured(20), "latitude": SECRET_LATITUDE}
        steps = [
            ("CLOCK_IN", at_office, "verified", []),
            ("BREAK_START", {"status": "permission_denied"}, "unverified", ["permission_denied"]),
            ("BREAK_END", captured(2000), "outside_radius", []),
            ("CLOCK_OUT", captured(30, accuracy=900), "inconclusive", ["low_accuracy"]),
        ]
        for event_type, location, outcome, reasons in steps:
            done = await press(client, event_type, location)
            assert done.status_code == 200, done.text
            shown = done.json()["day"]["events"][-1]["location"]
            assert shown == {"outcome": outcome, "reason_codes": reasons}, event_type
        # An older client that sends nothing is unverified, not refused.
        assert (await press(client, "CLOCK_IN", None)).status_code in (200, 409)
    stored = await evidence_of(api, t.employee)
    assert [e.outcome for e in stored[:4]] == ["verified", "unverified", "outside_radius", "inconclusive"]
    assert [e.review_required for e in stored[:4]] == [False, True, True, True]
    # About 13.7 m north of the office, stored to the nearest 10 m.
    assert stored[0].distance_m == 10
    assert (stored[2].distance_m, stored[2].distance_over_cap) == (2000, False)
    assert stored[3].accuracy_m == 900
    assert stored[1].distance_m is None
    # No coordinates anywhere: not in the evidence, not in the audit log, not in the logs.
    raw = await api.find("attendance_location_evidence", {"employee_id": t.employee.employee_id})
    assert not {key for doc in raw for key in doc} & {"latitude", "longitude", "position", "coordinates"}
    audit = await api.find("audit_log", {"subject_employee_id": t.employee.employee_id})
    assert str(SECRET_LATITUDE) not in json.dumps(audit + raw, default=str)
    events = await api.find("attendance_events", {"employee_id": t.employee.employee_id})
    assert str(SECRET_LATITUDE) not in json.dumps(events, default=str)
    assert all(
        str(SECRET_LATITUDE) not in record.getMessage() + str(record.__dict__) for record in caplog.records
    )
    # The day's figures are judged from the events alone, as before.
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        day = (await client.get("/api/v1/me/attendance/today")).json()["day"]
        assert [e["event_type"] for e in day["events"]][:4] == [
            "CLOCK_IN",
            "BREAK_START",
            "BREAK_END",
            "CLOCK_OUT",
        ]


async def test_ordinary_exports_carry_no_location_data(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        await press(client, "CLOCK_IN", captured(2000))
    assert not [column for column in (*exports.DAYS_HEADER, *exports.SUMMARY_HEADER) if "location_" in column]
    day = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        rows = await exports.day_rows(session, [t.employee.employee_id], day, day, api.clock.now())  # type: ignore[list-item]
    assert "outside_radius" not in json.dumps(rows, default=str)


async def test_a_repeated_request_leaves_one_event_and_one_evidence(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        key = f"test-{uuid.uuid4()}"
        results = await asyncio.gather(*(press(client, "CLOCK_IN", captured(10), key) for _ in range(3)))
        assert {r.status_code for r in results} <= {200, 409}
        assert any(r.status_code == 200 for r in results)
        # Two different presses at once: the sequence lets one through.
        racing = await asyncio.gather(
            press(client, "CLOCK_OUT", captured(10)), press(client, "CLOCK_OUT", captured(10))
        )
        assert sorted(r.status_code for r in racing)[0] == 200
    assert await api.count("attendance_events", {"employee_id": t.employee.employee_id}) == 2
    assert len(await evidence_of(api, t.employee)) == 2


async def test_several_approved_offices_and_their_dates(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    async with api.database.unit_of_work() as session:
        second = await create_org_units(session)
    far = (OFFICE[0] + 5000 / M_PER_DEGREE, OFFICE[1])
    tomorrow = (api.clock.now().date() + timedelta(days=1)).isoformat()
    async with api.client() as client:
        await signed_in(api, client, t.admin)
        created = await client.post(
            "/api/v1/attendance/location-checks", json=check_body(second.location_id, today(api), at=far)
        )
        assert created.status_code == 201, created.text
        # HR approves the second office for the employee, from today.
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        approved = await client.post(
            "/api/v1/attendance/office-approvals",
            json={
                "employee_id": str(t.employee.employee_id),
                "location_id": str(second.location_id),
                "effective_from": today(api),
            },
        )
        assert approved.status_code == 201, approved.text
        again = await client.post(
            "/api/v1/attendance/office-approvals",
            json={
                "employee_id": str(t.employee.employee_id),
                "location_id": str(second.location_id),
                "effective_from": today(api),
            },
        )
        assert (again.status_code, again.json()["code"]) == (409, "location.already_approved")
        listed = await client.get(
            "/api/v1/attendance/office-approvals", params={"employee_id": str(t.employee.employee_id)}
        )
        assert [item["location_id"] for item in listed.json()["items"]] == [str(second.location_id)]
        ended = await client.post(
            f"/api/v1/attendance/office-approvals/{approved.json()['id']}/end",
            json={"effective_to": tomorrow},
        )
        assert ended.status_code == 200, ended.text
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        done = await press(client, "CLOCK_IN", captured(20, at=far))
        assert done.json()["day"]["events"][-1]["location"]["outcome"] == "verified"
    [first] = await evidence_of(api, t.employee)
    assert (first.location_id, first.offices_considered) == (second.location_id, 2)
    actions = {
        row.action
        for row in await api.records(AuditLogRecord, {"subject_employee_id": t.employee.employee_id})
    }
    assert {"attendance.office_approval.created", "attendance.office_approval.ended"} <= actions
    # Archiving the second office takes it out of the choice at once.
    await api.update("locations", {"_id": second.location_id}, {"status": "archived"})
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        done = await press(client, "CLOCK_OUT", captured(20, at=far))
        assert done.json()["day"]["events"][-1]["location"]["outcome"] == "outside_radius"
    latest = (await evidence_of(api, t.employee))[-1]
    assert (latest.location_id, latest.offices_considered) == (t.units.location_id, 1)


async def test_checks_are_versioned_from_today_and_can_be_turned_off(api: Api) -> None:
    t = await team(api)
    first = await configure(api, t)
    tomorrow = (api.clock.now().date() + timedelta(days=1)).isoformat()
    yesterday = (api.clock.now().date() - timedelta(days=1)).isoformat()
    async with api.client() as client:
        await signed_in(api, client, t.admin)
        past = await client.post(
            "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, yesterday)
        )
        assert past.status_code == 422
        off = await client.post(
            "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, tomorrow, mode="off")
        )
        assert off.status_code == 201, off.text
        bad = await client.post(
            "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, tomorrow, radius_m=5)
        )
        assert bad.status_code == 422
        versions = [
            item
            for item in (await client.get("/api/v1/attendance/location-checks")).json()["items"]
            if item["location_id"] == str(t.units.location_id)
        ]
        assert [(v["mode"], v["effective_to"]) for v in versions] == [("off", None), ("verify", tomorrow)]
        assert versions[1]["id"] == first["id"]
    rows = await api.records(
        AuditLogRecord, {"action": "attendance.location_check.created", "target_id": first["id"]}
    )
    assert len(rows) == 1
    # From tomorrow, nothing is asked.
    api.clock.advance(timedelta(days=1))
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        assert (await client.get("/api/v1/me/attendance/today")).json()["location_check"] == "off"


async def flagged(api: Api, t: Team) -> str:
    await configure(api, t)
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        assert (await press(client, "CLOCK_IN", captured(2000))).status_code == 200
    [evidence] = await evidence_of(api, t.employee)
    return str(evidence.id)


async def test_hr_reviews_within_scope_without_changing_attendance(api: Api) -> None:
    t = await team(api)
    evidence_id = await flagged(api, t)
    before = await api.find("attendance_events", {"employee_id": t.employee.employee_id})
    path = f"/api/v1/attendance/location-reviews/{evidence_id}"
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        queue = (await client.get("/api/v1/attendance/location-reviews")).json()["items"]
        assert evidence_id in [item["id"] for item in queue]
        row = next(item for item in queue if item["id"] == evidence_id)
        assert (row["outcome"], row["distance_m"], row["current_review"]) == ("outside_radius", 2000, None)
        detail = await client.get(path)
        assert detail.status_code == 200, detail.text
        assert [e["event_type"] for e in detail.json()["day_events"]] == ["CLOCK_IN"]
        mismatch = await client.post(
            f"{path}/decisions",
            json={"decision": "accepted", "reason_code": "not_on_site_no_action", "supersedes": None},
        )
        assert mismatch.status_code == 422
        first = await client.post(
            f"{path}/decisions",
            json={
                "decision": "accepted",
                "reason_code": "on_site_confirmed",
                "note": "Gate log checked",
                "supersedes": None,
            },
        )
        assert first.status_code == 201, first.text
        stale = await client.post(
            f"{path}/decisions", json={"decision": "not_accepted", "reason_code": "other", "supersedes": None}
        )
        assert (stale.status_code, stale.json()["code"]) == (409, "location.review_stale")
        second = await client.post(
            f"{path}/decisions",
            json={
                "decision": "not_accepted",
                "reason_code": "not_on_site_correction_needed",
                "supersedes": first.json()["id"],
            },
        )
        assert second.status_code == 201, second.text
        history = (await client.get(path)).json()["reviews"]
        assert [r["decision"] for r in history] == ["accepted", "not_accepted"]
        still_open = (await client.get("/api/v1/attendance/location-reviews")).json()["items"]
        assert evidence_id not in [item["id"] for item in still_open]
        reviewed = (
            await client.get("/api/v1/attendance/location-reviews", params={"status": "reviewed"})
        ).json()
        assert evidence_id in [item["id"] for item in reviewed["items"]]
        # HR sees the outcome on the employee's day; nothing else changed.
        days = await client.get(
            f"/api/v1/employees/{t.employee.employee_id}/attendance/days",
            params={"from": today(api), "to": today(api)},
        )
        assert days.json()["items"][0]["events"][0]["location"]["outcome"] == "outside_radius"
    assert await api.find("attendance_events", {"employee_id": t.employee.employee_id}) == before
    actions = [
        row.action
        for row in await api.records(
            AuditLogRecord, {"target_id": evidence_id}, sort=[("recorded_at", 1), ("id", 1)]
        )
    ]
    assert actions.count("attendance.location.reviewed") == 2
    assert "attendance.location.evidence_read" in actions
    # A verified event has nothing to review.
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        await press(client, "CLOCK_OUT", captured(10))
    verified = (await evidence_of(api, t.employee))[-1]
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        nothing = await client.post(
            f"/api/v1/attendance/location-reviews/{verified.id}/decisions",
            json={"decision": "accepted", "reason_code": "on_site_confirmed", "supersedes": None},
        )
        assert (nothing.status_code, nothing.json()["code"]) == (409, "location.review_not_needed")


async def test_who_may_see_and_review_location_outcomes(api: Api) -> None:
    t = await team(api)
    evidence_id = await flagged(api, t)
    path = f"/api/v1/attendance/location-reviews/{evidence_id}"
    span = {"from": today(api), "to": today(api)}
    # The manager sees the team's day but no location outcome, and no review queue.
    async with api.client() as client:
        await api.sign_in(client, t.manager)
        days = await client.get(f"/api/v1/employees/{t.employee.employee_id}/attendance/days", params=span)
        assert days.status_code == 200
        assert days.json()["items"][0]["events"][0]["location"] is None
        assert (await client.get("/api/v1/attendance/location-reviews")).status_code == 403
    # The employee sees their own outcome, never another's evidence.
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        own = await client.get("/api/v1/me/attendance/days", params=span)
        assert own.json()["items"][0]["events"][0]["location"]["outcome"] == "outside_radius"
        assert (await client.get(path)).status_code == 403
    for roles in (
        (RoleKey.PAYROLL_ADMIN.value,),
        (RoleKey.SYSTEM_ADMIN.value,),
        (RoleKey.SUPER_ADMIN.value,),
        (RoleKey.AUDITOR.value,),
    ):
        actor = await api.create_account(roles=roles)
        async with api.client() as client:
            await signed_in(api, client, actor)
            assert (await client.get("/api/v1/attendance/location-reviews")).status_code == 403, roles
            assert (
                await client.post(
                    "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, today(api))
                )
            ).status_code == 403
    # HR configures nothing (HR admins do) but approves offices and reviews.
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        assert (
            await client.post(
                "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, today(api))
            )
        ).status_code == 403
    # HR limited to another department neither sees nor reviews it.
    async with api.database.unit_of_work() as session:
        other = await create_org_units(session)
    narrow = await api.create_account(
        roles=(RoleKey.HR.value,), role_scope={"department_id": other.department_id}
    )
    async with api.client() as client:
        await signed_in(api, client, narrow)
        assert evidence_id not in [
            item["id"] for item in (await client.get("/api/v1/attendance/location-reviews")).json()["items"]
        ]
        assert (await client.get(path)).status_code == 404


async def test_nobody_reviews_their_own_evidence(api: Api) -> None:
    t = await team(api)
    await configure(api, t)
    today_date = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        own_record = await create_employee(session, joined=today_date - timedelta(days=60))
        await add_job(
            session, t.units, own_record, manager_id=None, effective_from=today_date - timedelta(days=60)
        )
    hr_employee = await api.create_account(roles=(RoleKey.HR.value,), employee_id=own_record)
    async with api.client() as client:
        await signed_in(api, client, hr_employee)
        assert (await press(client, "CLOCK_IN", {"status": "skipped"})).status_code == 200
        [mine] = await api.records(LocationEvidence, {"employee_id": own_record})
        listed = (await client.get("/api/v1/attendance/location-reviews", params={"status": "all"})).json()[
            "items"
        ]
        assert str(mine.id) not in [item["id"] for item in listed]
        detail = await client.get(f"/api/v1/attendance/location-reviews/{mine.id}")
        assert (detail.status_code, detail.json()["code"]) == (403, "sod.own_request")
        decided = await client.post(
            f"/api/v1/attendance/location-reviews/{mine.id}/decisions",
            json={"decision": "accepted", "reason_code": "device_or_signal_issue", "supersedes": None},
        )
        assert (decided.status_code, decided.json()["code"]) == (403, "sod.own_request")


async def test_evidence_and_reviews_are_append_only_and_validated(api: Api) -> None:
    t = await team(api)
    evidence_id = await flagged(api, t)
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        await client.post(
            f"/api/v1/attendance/location-reviews/{evidence_id}/decisions",
            json={"decision": "accepted", "reason_code": "on_site_confirmed", "supersedes": None},
        )
    with pytest.raises(AppendOnlyError):  # noqa: PT012 (the commit raises)
        async with api.database.unit_of_work() as session:
            [evidence] = await session.find(LocationEvidence, {"id": uuid.UUID(evidence_id)})
            evidence.outcome = "verified"
    for collection in ("attendance_location_evidence", "attendance_location_reviews"):
        scoped = {"organization_id": api.organization_id, "employee_id": t.employee.employee_id}
        with pytest.raises(OperationFailure, match="not authorized"):
            await api.database.collection(collection).update_one(scoped, {"$set": {"note": "x"}})
        with pytest.raises(OperationFailure, match="not authorized"):
            await api.database.collection(collection).delete_many(scoped)
    # The validator refuses an outcome without the measurements it needs.
    [stored] = await api.records(LocationEvidence, {"id": uuid.UUID(evidence_id)})
    forged = to_document(stored)
    forged.update({"_id": uuid.uuid4(), "event_id": uuid.uuid4(), "outcome": "verified", "distance_m": None})
    with pytest.raises(WriteError, match="Document failed validation"):
        await api.database.collection("attendance_location_evidence").insert_one(forged)
    assert len(await api.records(LocationReview, {"evidence_id": uuid.UUID(evidence_id)})) == 1


async def test_erasure_reports_location_records_as_kept(api: Api) -> None:
    t = await team(api)
    await flagged(api, t)
    async with api.database.unit_of_work() as session:
        result = await erase_employee(session, t.employee.employee_id)  # type: ignore[arg-type]
    categories = {c.key: (c.outcome.value, c.count) for c in result.categories}
    assert categories["attendance.location_evidence"] == ("retained", 1)
    assert categories["attendance.location_reviews"] == ("retained", 0)
    assert categories["attendance.office_approvals"] == ("retained", 0)


async def test_another_organizations_offices_and_evidence_stay_out_of_reach(
    migrated_mongo: MongoServer,
) -> None:
    async with (
        running_api(migrated_mongo, location_checks_enabled=True) as a,
        running_api(migrated_mongo, location_checks_enabled=True) as b,
    ):
        t = await team(a)
        evidence_id = await flagged(a, t)
        b_admin = await b.create_account(roles=(RoleKey.HR_ADMIN.value,))
        b.clock.current = max(a.clock.current, b.clock.current)
        async with b.client() as client:
            await signed_in(b, client, b_admin)
            assert (await client.get(f"/api/v1/attendance/location-reviews/{evidence_id}")).status_code == 404
            assert (await client.get("/api/v1/attendance/location-reviews", params={"status": "all"})).json()[
                "items"
            ] == []
            foreign = await client.post(
                "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, today(b))
            )
            assert foreign.status_code == 422
            approval = await client.post(
                "/api/v1/attendance/office-approvals",
                json={
                    "employee_id": str(t.employee.employee_id),
                    "location_id": str(t.units.location_id),
                    "effective_from": today(b),
                },
            )
            assert approval.status_code == 404
            checks = (await client.get("/api/v1/attendance/location-checks")).json()["items"]
            assert str(t.units.location_id) not in [item["location_id"] for item in checks]
        assert len(await a.records(AttendanceEvent, {"employee_id": t.employee.employee_id})) == 1
