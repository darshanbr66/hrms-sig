"""A working day declared in the holiday calendar (revision 0027): the shift keeps its weekly
offs, and only the declared date is worked, for attendance judging and leave day counting.

Every shift time, policy value and date is an obviously fake test value (docs/database-design.md §10).
"""

from datetime import date, timedelta

import httpx

from tests.api_support import Api
from tests.integration.test_attendance_rules_api import Team, days, local_today, policy_body, team
from tests.people_data import fake_code

SATURDAY = 6


def saturday_before(day: date) -> date:
    return day - timedelta(days=(day.isoweekday() - SATURDAY) % 7 or 7)


def saturday_after(day: date) -> date:
    return day + timedelta(days=(SATURDAY - day.isoweekday()) % 7 or 7)


async def weekday_shift(client: httpx.AsyncClient, t: Team, start: date) -> None:
    """A test shift with Saturdays and Sundays off, as the location default, and a policy."""
    shift = await client.post(
        "/api/v1/shifts",
        json={
            "code": fake_code("SH"),
            "name": "Test weekday shift",
            "version": {
                "effective_from": start.isoformat(),
                "start_time": "09:00",
                "end_time": "17:00",
                "weekly_offs": [{"weekday": 6}, {"weekday": 7}],
            },
        },
    )
    assert shift.status_code == 201, shift.text
    default = await client.post(
        "/api/v1/location-default-shifts",
        json={
            "location_id": str(t.units.location_id),
            "shift_id": shift.json()["id"],
            "effective_from": start.isoformat(),
        },
    )
    assert default.status_code == 201, default.text
    policy = await client.post("/api/v1/attendance-policies", json=policy_body(t.units.location_id, start))
    assert policy.status_code == 201, policy.text


async def declare(client: httpx.AsyncClient, t: Team, saturdays: list[date]) -> list[str]:
    """One calendar per year of the given Saturdays, each Saturday declared a working day."""
    calendar_ids = []
    for year in sorted({day.year for day in saturdays}):
        created = await client.post(
            "/api/v1/holiday-calendars", json={"name": f"Test {fake_code()}", "year": year}
        )
        assert created.status_code == 201, created.text
        path = f"/api/v1/holiday-calendars/{created.json()['id']}"
        version = 1
        for day in (d for d in saturdays if d.year == year):
            added = await client.post(
                f"{path}/holidays",
                json={"date": day.isoformat(), "name": "Test working Saturday", "kind": "working_day"},
                headers={"If-Match": f'"{version}"'},
            )
            assert added.status_code == 201, added.text
            version += 1
        assigned = await client.put(
            f"{path}/locations/{t.units.location_id}", headers={"If-Match": f'"{version}"'}
        )
        assert assigned.status_code == 200, assigned.text
        calendar_ids.append(created.json()["id"])
    return calendar_ids


async def test_a_declared_working_saturday_is_judged_and_counted_while_other_saturdays_stay_off(
    api: Api,
) -> None:
    t = await team(api)
    today = local_today(api)
    worked = saturday_before(today)
    upcoming = saturday_after(today)
    async with api.client() as client:
        await api.sign_in(client, t.admin)
        await weekday_shift(client, t, api.clock.now().date() - timedelta(days=60))
        [first, *_] = await declare(client, t, [worked, upcoming])

        path = f"/api/v1/holiday-calendars/{first}"
        version = (await client.get(path)).headers["ETag"]
        optional = await client.post(
            f"{path}/holidays",
            json={"date": f"{worked.year}-01-02", "name": "Test", "kind": "working_day", "is_optional": True},
            headers={"If-Match": version},
        )
        assert optional.status_code == 422
        assert optional.json()["errors"][0]["field"] == "is_optional"
        taken = await client.post(
            f"{path}/holidays",
            json={"date": worked.isoformat(), "name": "Test holiday", "kind": "holiday"},
            headers={"If-Match": version},
        )
        assert taken.json()["errors"][0]["code"] == "holiday.date_taken"
        listed = (await client.get(path)).json()["holidays"]
        assert {(item["date"], item["kind"]) for item in listed} >= {(worked.isoformat(), "working_day")}
        summaries = (await client.get("/api/v1/holiday-calendars")).json()["items"]
        summary = next(item for item in summaries if item["id"] == first)
        declared_in_year = sum(day.year == worked.year for day in (worked, upcoming))
        assert (summary["holiday_count"], summary["working_day_count"]) == (0, declared_in_year)

        leave_type = await client.post(
            "/api/v1/leave-types",
            json={
                "code": fake_code("LT"),
                "name": "Test leave",
                "is_paid": True,
                "requires_approval": True,
                "allows_half_day": True,
                "counts_weekly_offs": False,
                "counts_holidays": False,
            },
        )
        assert leave_type.status_code == 201, leave_type.text
        leave_policy = await client.post(
            "/api/v1/leave-policies",
            json={
                "leave_type_id": leave_type.json()["id"],
                "location_id": str(t.units.location_id),
                "effective_from": "2020-01-01",
                "leave_year_start_month": 1,
                "leave_year_start_day": 1,
                "allow_negative_balance": False,
                "accrual_method": "upfront_per_leave_year",
                "accrual_amount": "20",
                "prorate_on_joining": False,
            },
        )
        assert leave_policy.status_code == 201, leave_policy.text

    async with api.client() as client:
        await api.sign_in(client, t.employee)
        # The declared Saturday is a working day with no events: absent, not a weekly off.
        declared = await days(client, "/api/v1/me/attendance/days", worked)
        assert declared["status"] == "absent"
        assert (await days(client, "/api/v1/me/attendance/days", worked - timedelta(days=7)))[
            "status"
        ] == "weekly_off"
        assert (await days(client, "/api/v1/me/attendance/days", worked + timedelta(days=1)))[
            "status"
        ] == "weekly_off"

        mine = await client.get(
            "/api/v1/me/holidays", params={"from": worked.isoformat(), "to": worked.isoformat()}
        )
        assert [(item["kind"], item["name"]) for item in mine.json()["items"]] == [
            ("working_day", "Test working Saturday")
        ]

        # Leave over the declared Saturday counts it; the Sunday after stays a weekly off.
        monday = upcoming - timedelta(days=5)
        preview = await client.post(
            "/api/v1/me/leave-requests/preview",
            json={
                "leave_type_id": leave_type.json()["id"],
                "start_date": monday.isoformat(),
                "end_date": (upcoming + timedelta(days=1)).isoformat(),
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["days_requested"] == "6.00"
        assert [d["not_counted_reason"] for d in preview.json()["days"] if not d["counted"]] == ["weekly_off"]

    [(changes,)] = await api.fetch(
        "SELECT changes FROM audit.audit_log WHERE action = 'holiday.added' AND target_id = :id "
        "AND changes->'date'->>'new' = :day",
        id=first,
        day=worked.isoformat(),
    )
    assert changes["kind"] == {"old": None, "new": "working_day"}
