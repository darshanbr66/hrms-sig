"""Exports over HTTP with private storage (product-requirements RPT-1 to RPT-3)."""

import csv
import io
from datetime import timedelta

from app.modules.reports.job import produce, purge
from app.platform.authz.roles import RoleKey
from app.platform.db import tenant_scope
from tests.api_support import Api
from tests.people_data import add_job, create_employee, create_org_units


async def test_an_export_covers_only_what_the_requester_reads_and_only_they_download_it(  # noqa: PLR0915
    files_api: Api,
) -> None:
    api = files_api
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        other_units = await create_org_units(session)
        inside = await create_employee(session, joined=today - timedelta(days=10))
        await add_job(session, units, inside, manager_id=None, effective_from=today - timedelta(days=10))
        outside = await create_employee(session, joined=today - timedelta(days=10))
        await add_job(
            session, other_units, outside, manager_id=None, effective_from=today - timedelta(days=10)
        )
    # HR for one location only.
    hr = await api.create_account(
        roles=(RoleKey.HR_ADMIN.value,), role_scope={"location_id": units.location_id}
    )
    other = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, hr)
        without_step_up = await client.post("/api/v1/exports/employees", json={})
        assert without_step_up.status_code == 403
        assert without_step_up.json()["type"] == "/problems/step-up-required"
        await api.step_up(client, hr)
        requested = await client.post("/api/v1/exports/employees", json={})
        assert requested.status_code == 202, requested.text
        export = requested.json()
        assert export["status"] == "queued"
        # One export in preparation at a time (api-architecture §9).
        in_progress = await client.post("/api/v1/exports/employees", json={})
        assert in_progress.status_code == 409, in_progress.text
        assert in_progress.json()["code"] == "export.in_progress"
        too_long = await client.post(
            "/api/v1/exports/attendance-days",
            json={"from": (today - timedelta(days=40)).isoformat(), "to": today.isoformat()},
        )
        assert too_long.json()["errors"][0]["code"] == "range.too_long"

    # As the worker runs them: once per organization, inside its scope.
    with tenant_scope(api.organization_id):
        assert await produce(api.worker_database, api.files, api.clock) >= 1
    async with api.client() as client:
        await api.sign_in(client, hr)
        mine = (await client.get("/api/v1/me/exports")).json()
        done = next(item for item in mine["items"] if item["id"] == export["id"])
        assert done["status"] == "done"
        assert done["row_count"] == 1
        link = (await client.post(f"/api/v1/me/exports/{export['id']}/download")).json()
        async with api.client(csrf=False) as plain:
            body = (await plain.get(link["url"])).content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(body)))
        assert len(rows) == 1
        assert rows[0]["employee_code"]
        notes = (await client.get("/api/v1/me/notifications")).json()["items"]
        assert any(n["type"] == "export.ready" for n in notes)
    async with api.client() as client:
        await api.sign_in(client, other)
        assert (await client.post(f"/api/v1/me/exports/{export['id']}/download")).status_code == 404
    audited = await api.count(
        "audit_log", {"target_id": export["id"], "action": {"$in": ["export.requested", "export.downloaded"]}}
    )
    assert audited == 2

    api.clock.advance(timedelta(hours=25))
    with tenant_scope(api.organization_id):
        assert await purge(api.worker_database, api.files, api.clock) >= 1
    async with api.client() as client:
        await api.sign_in(client, hr)
        expired = await client.post(f"/api/v1/me/exports/{export['id']}/download")
        assert expired.status_code == 409
        assert expired.json()["code"] == "export.not_available"
