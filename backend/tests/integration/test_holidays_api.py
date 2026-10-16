"""Holiday calendars over HTTP (docs/product-requirements.md LV-7). All data is obviously fake."""

import secrets
from datetime import date, timedelta

from app.platform.authz.roles import RoleKey
from tests.api_support import Api
from tests.people_data import add_job, create_employee, create_org_units


async def test_hr_admin_builds_a_calendar_and_employees_see_their_locations_holidays(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    today = api.clock.now().date()
    year = today.year
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(session, units, employee_id, manager_id=None, effective_from=today - timedelta(days=30))
    employee = await api.create_account(employee_id=employee_id)
    name = f"Test calendar {secrets.token_hex(3)}"
    holiday_date = date(year, 12, 31)

    async with api.client() as client:
        await api.sign_in(client, admin)
        created = await client.post("/api/v1/holiday-calendars", json={"name": name, "year": year})
        assert created.status_code == 201, created.text
        calendar = created.json()
        path = f"/api/v1/holiday-calendars/{calendar['id']}"

        duplicate = await client.post("/api/v1/holiday-calendars", json={"name": name, "year": year})
        assert duplicate.status_code == 409

        outside = await client.post(
            f"{path}/holidays",
            json={"date": f"{year + 1}-01-01", "name": "Test outside"},
            headers={"If-Match": '"1"'},
        )
        assert outside.status_code == 422

        added = await client.post(
            f"{path}/holidays",
            json={"date": holiday_date.isoformat(), "name": "Test holiday"},
            headers={"If-Match": '"1"'},
        )
        assert added.status_code == 201, added.text
        assert [item["name"] for item in added.json()["holidays"]] == ["Test holiday"]
        stale = await client.post(
            f"{path}/holidays",
            json={"date": f"{year}-12-30", "name": "Test stale"},
            headers={"If-Match": '"1"'},
        )
        assert stale.status_code == 412

        assigned = await client.put(f"{path}/locations/{units.location_id}", headers={"If-Match": '"2"'})
        assert assigned.status_code == 200, assigned.text
        assert [item["id"] for item in assigned.json()["locations"]] == [str(units.location_id)]

    async with api.client() as client:
        await api.sign_in(client, employee)
        mine = await client.get(
            "/api/v1/me/holidays", params={"from": f"{year}-01-01", "to": f"{year}-12-31"}
        )
        assert mine.status_code == 200
        assert mine.json()["items"] == [
            {
                "date": holiday_date.isoformat(),
                "name": "Test holiday",
                "is_optional": False,
                "kind": "holiday",
                "calendar": name,
            }
        ]
        assert (
            await client.post("/api/v1/holiday-calendars", json={"name": "No", "year": year})
        ).status_code == 403

    actions = await api.fetch(
        "SELECT action FROM audit.audit_log WHERE target_id = :id ORDER BY recorded_at, id", id=calendar["id"]
    )
    assert [row[0] for row in actions] == [
        "holiday.calendar.created",
        "holiday.added",
        "holiday.calendar.location_assigned",
    ]


async def test_a_location_uses_one_calendar_per_year(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    year = api.clock.now().year
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
    async with api.client() as client:
        await api.sign_in(client, admin)
        first = (
            await client.post(
                "/api/v1/holiday-calendars", json={"name": f"Test A {secrets.token_hex(3)}", "year": year}
            )
        ).json()
        second = (
            await client.post(
                "/api/v1/holiday-calendars", json={"name": f"Test B {secrets.token_hex(3)}", "year": year}
            )
        ).json()
        await client.put(
            f"/api/v1/holiday-calendars/{first['id']}/locations/{units.location_id}",
            headers={"If-Match": '"1"'},
        )
        moved = await client.put(
            f"/api/v1/holiday-calendars/{second['id']}/locations/{units.location_id}",
            headers={"If-Match": '"1"'},
        )
        assert moved.status_code == 200
        first_now = (await client.get(f"/api/v1/holiday-calendars/{first['id']}")).json()
        assert first_now["locations"] == []
