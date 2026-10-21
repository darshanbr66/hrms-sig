"""Editing, archiving and restoring configuration records over HTTP (docs/module-acceptance.md,
supported record actions): holiday calendar entries are edited in place but keep their date and
kind; a calendar a location still uses cannot be archived, and an archived calendar is read-only
until restored; a leave type cannot be archived while requests of it wait for a decision. Every
change is audited; nothing is deleted except a calendar entry, which is removed with its values
recorded. All data is obviously fake (docs/database-design.md §10).
"""

import secrets
from datetime import date

import httpx

from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from tests.api_support import Api
from tests.integration.test_leave_api import allocate, draft, key, world
from tests.people_data import create_org_units


async def calendar_with_entries(client: httpx.AsyncClient, year: int) -> tuple[str, dict[str, str]]:
    created = await client.post(
        "/api/v1/holiday-calendars", json={"name": f"Test {secrets.token_hex(3)}", "year": year}
    )
    assert created.status_code == 201, created.text
    path = f"/api/v1/holiday-calendars/{created.json()['id']}"
    entries = {}
    for version, (day, kind) in enumerate(
        ((date(year, 12, 30), "holiday"), (date(year, 12, 27), "working_day")), 1
    ):
        added = await client.post(
            f"{path}/holidays",
            json={"date": day.isoformat(), "name": f"Test {kind}", "kind": kind},
            headers={"If-Match": f'"{version}"'},
        )
        assert added.status_code == 201, added.text
        entries[kind] = next(item["id"] for item in added.json()["holidays"] if item["kind"] == kind)
    return path, entries


async def test_an_entry_is_edited_but_keeps_its_date_and_kind(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    year = api.clock.now().year + 1
    async with api.client() as client:
        await api.sign_in(client, admin)
        path, entries = await calendar_with_entries(client, year)
        holiday = f"{path}/holidays/{entries['holiday']}"

        stale = await client.patch(holiday, json={"name": "Test renamed"}, headers={"If-Match": '"1"'})
        assert stale.status_code == 412
        renamed = await client.patch(
            holiday, json={"name": "Test renamed", "is_optional": True}, headers={"If-Match": '"3"'}
        )
        assert renamed.status_code == 200, renamed.text
        entry = next(item for item in renamed.json()["holidays"] if item["id"] == entries["holiday"])
        assert (entry["name"], entry["is_optional"], entry["kind"], entry["date"]) == (
            "Test renamed",
            True,
            "holiday",
            f"{year}-12-30",
        )
        # The date and the kind are not fields: remove the entry and add the right one.
        moved = await client.patch(holiday, json={"date": f"{year}-12-29"}, headers={"If-Match": '"4"'})
        assert moved.status_code == 422
        optional_working_day = await client.patch(
            f"{path}/holidays/{entries['working_day']}",
            json={"is_optional": True},
            headers={"If-Match": '"4"'},
        )
        assert optional_working_day.status_code == 422
        assert optional_working_day.json()["errors"][0]["code"] == "holiday.working_day_optional"
        cleared = await client.patch(holiday, json={"name": None}, headers={"If-Match": '"4"'})
        assert cleared.status_code == 422

        # An entry of another calendar is not found through this one.
        other, other_entries = await calendar_with_entries(client, year + 1)
        crossed = await client.patch(
            f"{path}/holidays/{other_entries['holiday']}", json={"name": "Test"}, headers={"If-Match": '"4"'}
        )
        assert crossed.status_code == 404
        assert other

        listed = (await client.get("/api/v1/holiday-calendars", params={"year": year})).json()["items"]
        summary = next(item for item in listed if path.endswith(item["id"]))
        assert (summary["holiday_count"], summary["working_day_count"]) == (1, 1)

    async with api.client() as client:
        await api.sign_in(client, hr)
        # HR reads calendars but does not manage them.
        refused = await client.patch(holiday, json={"name": "Test"}, headers={"If-Match": '"4"'})
        assert refused.status_code == 403

    [updated] = await api.records(
        AuditLogRecord, {"action": "holiday.updated", "target_id": path.rsplit("/", 1)[1]}
    )
    changes, permission = updated.changes or {}, updated.permission_used
    assert permission == "holiday.manage"
    assert changes["name"] == {"old": "Test holiday", "new": "Test renamed"}
    assert changes["is_optional"] == {"old": False, "new": True}
    assert changes["date"] == {"old": f"{year}-12-30", "new": f"{year}-12-30"}


async def test_a_calendar_in_use_is_not_archived_and_an_archived_one_is_read_only(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
    year = api.clock.now().year + 1
    async with api.client() as client:
        await api.sign_in(client, admin)
        path, entries = await calendar_with_entries(client, year)
        location = f"{path}/locations/{units.location_id}"
        assert (await client.put(location, headers={"If-Match": '"3"'})).status_code == 200

        in_use = await client.patch(path, json={"status": "archived"}, headers={"If-Match": '"4"'})
        assert in_use.status_code == 409
        assert in_use.json()["code"] == "holiday.calendar_in_use"
        assert (await client.delete(location, headers={"If-Match": '"4"'})).status_code == 200
        archived = await client.patch(path, json={"status": "archived"}, headers={"If-Match": '"5"'})
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"

        for refused in (
            await client.post(
                f"{path}/holidays",
                json={"date": f"{year}-12-31", "name": "Test"},
                headers={"If-Match": '"6"'},
            ),
            await client.patch(
                f"{path}/holidays/{entries['holiday']}", json={"name": "Test"}, headers={"If-Match": '"6"'}
            ),
            await client.delete(f"{path}/holidays/{entries['holiday']}", headers={"If-Match": '"6"'}),
            await client.put(location, headers={"If-Match": '"6"'}),
        ):
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "holiday.calendar_archived"
        # The refused changes left the calendar as it was.
        unchanged = (await client.get(path)).json()
        assert (unchanged["version"], len(unchanged["holidays"])) == (6, 2)

        restored = await client.patch(path, json={"status": "active"}, headers={"If-Match": '"6"'})
        assert restored.status_code == 200, restored.text
        removed = await client.delete(f"{path}/holidays/{entries['holiday']}", headers={"If-Match": '"7"'})
        assert removed.status_code == 200, removed.text
        assert [item["kind"] for item in removed.json()["holidays"]] == ["working_day"]

    rows = await api.records(
        AuditLogRecord, {"target_id": path.rsplit("/", 1)[1]}, sort=[("recorded_at", 1), ("id", 1)]
    )
    statuses = [(row.changes or {})["status"] for row in rows if row.action == "holiday.calendar.updated"]
    assert statuses == [{"old": "active", "new": "archived"}, {"old": "archived", "new": "active"}]
    assert rows[-1].action == "holiday.removed"
    assert (rows[-1].changes or {})["name"] == {"old": "Test holiday", "new": None}


async def test_a_leave_type_with_requests_awaiting_a_decision_is_not_archived(api: Api) -> None:
    w = await world(api)
    await allocate(api, w)
    async with api.client() as client:
        await api.sign_in(client, w.employee)
        submitted = await client.post("/api/v1/me/leave-requests", json=draft(w, 1), headers=key())
        assert submitted.status_code == 201, submitted.text

    path = f"/api/v1/leave-types/{w.leave_type_id}"
    async with api.client() as client:
        await api.sign_in(client, w.admin)
        refused = await client.patch(path, json={"status": "archived"}, headers={"If-Match": '"1"'})
        assert refused.status_code == 409
        assert refused.json()["code"] == "leave.type_in_use"
        assert "1 request of this leave type waits" in refused.json()["detail"]

    async with api.client() as client:
        await api.sign_in(client, w.employee)
        cancelled = await client.post(f"/api/v1/me/leave-requests/{submitted.json()['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text

    async with api.client() as client:
        await api.sign_in(client, w.admin)
        archived = await client.patch(path, json={"status": "archived"}, headers={"If-Match": '"1"'})
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"

    async with api.client() as client:
        await api.sign_in(client, w.employee)
        # An archived type takes no new requests.
        closed = await client.post("/api/v1/me/leave-requests", json=draft(w, 1), headers=key())
        assert closed.status_code in (404, 409, 422), closed.text

    [updated] = await api.records(
        AuditLogRecord, {"action": "leave.type.updated", "target_id": w.leave_type_id}
    )
    changes = updated.changes or {}
    assert changes["status"] == {"old": "active", "new": "archived"}
