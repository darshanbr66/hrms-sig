"""The installation switch for office location verification (`LOCATION_CHECKS_ENABLED`, ADR-031).

Off unless turned on, in every environment. While off, no office check can be created or
versioned through the API, whoever asks, and no check applies to clock events even if one is
stored: nothing is asked of browsers, no evidence is written, and clocking works as without the
module. No role or setting can turn it on; only the environment can. Coordinates and limits are
obviously fake test values (docs/database-design.md §10).
"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from pydantic import SecretStr

from app.modules.attendance.models import LocationCheck
from app.platform.authz.roles import RoleKey
from app.platform.config import ApiSettings
from tests.api_support import Api, api_settings, running_api
from tests.conftest import MongoServer
from tests.integration.test_attendance_rules_api import Team, team
from tests.integration.test_location_verification import captured, check_body, press, signed_in, today


@pytest.fixture
async def off_api(migrated_mongo: MongoServer) -> AsyncIterator[Api]:
    """An installation with the switch as it is by default: off."""
    async with running_api(migrated_mongo) as running:
        yield running


def test_the_switch_is_off_unless_turned_on() -> None:
    assert ApiSettings.model_fields["location_checks_enabled"].default is False
    settings = api_settings(SecretStr("mongodb://127.0.0.1:27017/?directConnection=true"))
    assert settings.location_checks_enabled is False


async def stored_check(api: Api, t: Team) -> None:
    """A check as an earlier, enabled period could have left it."""
    async with api.database.unit_of_work() as session:
        session.add(
            LocationCheck(
                location_id=t.units.location_id,
                effective_from=api.clock.now().date() - timedelta(days=1),
                mode="verify",
                latitude=12.0,
                longitude=77.0,
                radius_m=150,
                max_accuracy_m=100,
                max_position_age_seconds=120,
                review_unverified=True,
                created_by=t.admin.user_id,
            )
        )


async def test_while_off_no_check_can_be_created_or_versioned_by_anyone(off_api: Api) -> None:
    api = off_api
    t = await team(api)
    tomorrow = (api.clock.now().date() + timedelta(days=1)).isoformat()
    async with api.client() as client:
        await signed_in(api, client, t.admin)
        listed = (await client.get("/api/v1/attendance/location-checks")).json()
        assert listed["checks_enabled"] is False
        for body in (
            check_body(t.units.location_id, today(api)),
            check_body(t.units.location_id, tomorrow),
            check_body(t.units.location_id, tomorrow, mode="off"),
        ):
            refused = await client.post("/api/v1/attendance/location-checks", json=body)
            assert refused.status_code == 503, refused.text
            assert refused.json()["code"] == "location.checks_disabled"
    # Who may not configure checks still learns only that.
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        hr = await client.post(
            "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, today(api))
        )
        assert hr.status_code == 403
    assert await api.count("attendance_location_checks", {}) == 0


async def test_no_role_can_turn_the_switch_on(off_api: Api) -> None:
    api = off_api
    t = await team(api)
    for roles in ((RoleKey.HR_ADMIN.value,), (RoleKey.SYSTEM_ADMIN.value,), (RoleKey.SUPER_ADMIN.value,)):
        actor = await api.create_account(roles=roles)
        async with api.client() as client:
            await signed_in(api, client, actor)
            for key in (
                "location_checks_enabled",
                "attendance.location_checks_enabled",
                "location.checks_enabled",
            ):
                attempt = await client.put(f"/api/v1/settings/{key}", json={"value": 1})
                assert attempt.status_code in (403, 404, 422), (roles, key, attempt.text)
            refused = await client.post(
                "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, today(api))
            )
            assert refused.status_code in (403, 503), roles
    assert await api.count("attendance_location_checks", {}) == 0
    assert await api.count("organization_settings", {"key": {"$regex": "location"}}) == 0


async def test_while_off_clocking_works_and_a_stored_check_applies_to_nobody(off_api: Api) -> None:
    api = off_api
    t = await team(api)
    await stored_check(api, t)
    async with api.client() as client:
        await api.sign_in(client, t.employee)
        assert (await client.get("/api/v1/me/attendance/today")).json()["location_check"] == "off"
        # An older or misbehaving client sending a position anyway, or a failure, or nothing.
        for event_type, location in (
            ("CLOCK_IN", captured(5000)),
            ("BREAK_START", {"status": "permission_denied"}),
            ("BREAK_END", None),
            ("CLOCK_OUT", captured(10)),
        ):
            done = await press(client, event_type, location)
            assert done.status_code == 200, done.text
            assert done.json()["day"]["events"][-1]["location"] is None
        day = (await client.get("/api/v1/me/attendance/today")).json()["day"]
        assert [e["event_type"] for e in day["events"]] == [
            "CLOCK_IN",
            "BREAK_START",
            "BREAK_END",
            "CLOCK_OUT",
        ]
    assert await api.count("attendance_events", {"employee_id": t.employee.employee_id}) == 4
    assert await api.count("attendance_location_evidence", {}) == 0
    # Nothing to review.
    async with api.client() as client:
        await signed_in(api, client, t.hr)
        queue = await client.get("/api/v1/attendance/location-reviews", params={"status": "all"})
        assert queue.json()["items"] == []


async def test_turned_on_checks_can_be_created_and_apply(migrated_mongo: MongoServer) -> None:
    async with running_api(migrated_mongo, location_checks_enabled=True) as api:
        t = await team(api)
        async with api.client() as client:
            await signed_in(api, client, t.admin)
            assert (await client.get("/api/v1/attendance/location-checks")).json()["checks_enabled"] is True
            created = await client.post(
                "/api/v1/attendance/location-checks", json=check_body(t.units.location_id, today(api))
            )
            assert created.status_code == 201, created.text
        async with api.client() as client:
            await api.sign_in(client, t.employee)
            assert (await client.get("/api/v1/me/attendance/today")).json()["location_check"] == "requested"
            done = await press(client, "CLOCK_IN", captured(10))
            assert done.json()["day"]["events"][-1]["location"]["outcome"] == "verified"
        assert await api.count("attendance_location_evidence", {}) == 1
