"""Tenant isolation over HTTP (docs/authorization-model.md §1.1, ADR-030): two organizations in one
database, and an administrator of one trying every way to reach the other's records by their
identifiers: reading, listing, searching, changing, removing, downloading and exporting.

Whatever the role, another organization's record answers exactly as a record that does not exist
(404), and lists, searches and exports never contain it. Organization B's accounts are signed in
to organization A's own API instance too, as one deployment serves every organization.
"""

import csv
import io
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest

from app.modules.people.models import Employee
from app.modules.reports.job import produce
from app.platform.authz.roles import RoleKey
from app.platform.db import tenant_scope
from app.platform.scanning import Verdict
from tests.api_support import Account, Api, running_api
from tests.conftest import MongoServer
from tests.integration.test_documents_api import category, scan, upload
from tests.people_data import add_job, create_employee, create_org_units


@dataclass
class Tenant:
    api: Api
    admin: Account
    system_admin: Account
    auditor: Account
    employee_id: uuid.UUID
    employee_account: Account
    location_id: uuid.UUID
    document_id: str
    version_id: str
    export_id: str


async def tenant(api: Api) -> Tenant:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(session, units, employee_id, manager_id=None, effective_from=today - timedelta(days=30))
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    system_admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    auditor = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    employee_account = await api.create_account(employee_id=employee_id)
    async with api.client() as client:
        await api.sign_in(client, admin)
        await api.step_up(client, admin)
        internal = await category(client, "internal", visible=True, upload=True)
        uploaded = await upload(client, f"/api/v1/employees/{employee_id}/documents", internal)
        assert uploaded.status_code == 201, uploaded.text
        document = uploaded.json()
        export = await client.post("/api/v1/exports/employees", json={})
        assert export.status_code == 202, export.text
    await scan(api, Verdict.CLEAN)
    with tenant_scope(api.organization_id):
        assert await produce(api.worker_database, api.files, api.clock) >= 1
    return Tenant(
        api,
        admin,
        system_admin,
        auditor,
        employee_id,
        employee_account,
        units.location_id,
        document["id"],
        document["versions"][0]["id"],
        export.json()["id"],
    )


@pytest.fixture
async def tenants(migrated_mongo: MongoServer) -> AsyncIterator[tuple[Tenant, Tenant]]:
    async with (
        running_api(migrated_mongo, document_uploads_enabled=True) as first,
        running_api(migrated_mongo, document_uploads_enabled=True) as second,
    ):
        assert first.organization_id != second.organization_id
        yield await tenant(first), await tenant(second)


def unknown_ids(path: str, a: Tenant) -> str:
    """The same path with every identifier of A replaced by one that exists nowhere."""
    for known in (
        a.employee_id,
        a.location_id,
        a.admin.user_id,
        a.employee_account.user_id,
        a.document_id,
        a.version_id,
    ):
        path = path.replace(str(known), str(uuid.uuid4()))
    return path


def empty(response: httpx.Response) -> object:
    """A response body with its request-specific parts removed, for comparing two answers."""
    body = response.json()
    if isinstance(body, dict):
        return {key: value for key, value in body.items() if key not in ("instance", "request_id")}
    return body


async def signed_in(api: Api, client: httpx.AsyncClient, account: Account) -> None:
    await api.sign_in(client, account)
    await api.step_up(client, account)


async def test_another_organizations_records_do_not_exist_for_an_administrator(
    tenants: tuple[Tenant, Tenant],
) -> None:
    a, b = tenants
    # B's administrators, through either instance.
    for api, administrator in ((a.api, b.admin), (b.api, b.admin), (b.api, b.system_admin)):
        # One account across two instances: their clocks must agree, or the second sign-in reuses
        # an authenticator step the first one consumed (refused as a replay).
        api.clock.current = max(a.api.clock.current, b.api.clock.current)
        async with api.client() as client:
            await signed_in(api, client, administrator)
            employee = f"/api/v1/employees/{a.employee_id}"
            for method, path, body in (
                ("GET", employee, None),
                ("GET", f"{employee}/personal", None),
                ("GET", f"{employee}/jobs", None),
                ("GET", f"{employee}/status-changes", None),
                ("GET", f"{employee}/emergency-contacts", None),
                ("GET", f"{employee}/documents", None),
                ("GET", f"{employee}/leave/balances", None),
                ("GET", f"{employee}/leave/ledger", None),
                ("GET", f"/api/v1/locations/{a.location_id}", None),
                ("GET", f"/api/v1/users/{a.admin.user_id}", None),
                ("GET", f"/api/v1/users/{a.admin.user_id}/sessions", None),
                ("GET", f"/api/v1/documents/{a.document_id}", None),
                ("POST", f"/api/v1/documents/{a.document_id}/versions/{a.version_id}/download", None),
                ("POST", f"/api/v1/users/{a.employee_account.user_id}/disable", {"reason": "Test"}),
                ("POST", f"{employee}/exit", {"last_working_day": "2030-01-01", "reason": "Test"}),
                ("DELETE", f"/api/v1/users/{a.admin.user_id}/sessions", None),
            ):
                response = await client.request(method, path, json=body)
                # Exactly the answer for an identifier that exists nowhere: no data, no oracle.
                unknown = await client.request(method, unknown_ids(path, a), json=body)
                assert (response.status_code, empty(response)) == (unknown.status_code, empty(unknown)), (
                    method,
                    path,
                    response.text,
                    unknown.text,
                )
                if response.status_code == 200:
                    assert response.json()["items"] == [], (method, path, response.text)
            for path in (
                employee,
                f"/api/v1/locations/{a.location_id}",
                f"/api/v1/documents/{a.document_id}",
            ):
                change = {"json": {"name": "Taken"}, "headers": {"If-Match": '"1"'}}
                changed = await client.patch(path, **change)  # type: ignore[arg-type]
                unknown = await client.patch(unknown_ids(path, a), **change)  # type: ignore[arg-type]
                assert (changed.status_code, empty(changed)) == (unknown.status_code, empty(unknown)), path
                assert changed.status_code in (403, 404, 422), (path, changed.text)
            # Lists and searches hold only B's own records.
            if administrator is b.admin:
                listed = (await client.get("/api/v1/employees")).json()["items"]
                assert str(a.employee_id) not in {item["id"] for item in listed}
                assert str(b.employee_id) in {item["id"] for item in listed}
            else:
                users = (await client.get("/api/v1/users")).json()["items"]
                assert not {str(a.admin.user_id), str(a.auditor.user_id)} & {item["id"] for item in users}
                assert str(b.admin.user_id) in {item["id"] for item in users}
    # Nothing of A changed.
    async with a.api.client() as client:
        await signed_in(a.api, client, a.admin)
        assert (await client.get(f"/api/v1/employees/{a.employee_id}")).status_code == 200
        assert (await client.get(f"/api/v1/users/{a.employee_account.user_id}")).json()["status"] == "active"


async def test_another_organizations_files_and_exports_stay_out_of_reach(
    tenants: tuple[Tenant, Tenant],
) -> None:
    a, b = tenants
    async with b.api.client() as client:
        await signed_in(b.api, client, b.admin)
        assert (await client.post(f"/api/v1/me/exports/{a.export_id}/download")).status_code == 404
        mine = (await client.get("/api/v1/me/exports")).json()["items"]
        assert [item["id"] for item in mine] == [b.export_id]
        # B's export holds B's employees only.
        link = (await client.post(f"/api/v1/me/exports/{b.export_id}/download")).json()["url"]
        rows = list(csv.DictReader(io.StringIO((await client.get(link)).content.decode("utf-8-sig"))))
        assert len(rows) == 1
    # A link signed for A's file names A's organization; it cannot be bent towards B's.
    async with a.api.client() as client:
        await signed_in(a.api, client, a.admin)
        link = (
            await client.post(f"/api/v1/documents/{a.document_id}/versions/{a.version_id}/download")
        ).json()["url"]
    async with b.api.client(csrf=False) as anonymous:
        assert (await anonymous.get(link)).status_code == 200  # the signed link is the authorization
        payload, _, signature = link.rpartition("/")[2].partition(".")
        assert (await anonymous.get(f"/api/v1/files/{payload}x.{signature}")).status_code == 404


async def test_audit_and_people_searches_never_cross_organizations(tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    async with b.api.client() as client:
        await signed_in(b.api, client, b.auditor)
        entries = (await client.get("/api/v1/audit-log", params={"limit": 100})).json()["items"]
        assert entries
        actors = {item.get("actor_user_id") for item in entries}
        assert str(a.admin.user_id) not in actors
        assert str(b.admin.user_id) in actors
    async with b.api.client() as client:
        await signed_in(b.api, client, b.employee_account)
        [stored] = await a.api.records(Employee, {"id": a.employee_id})
        found = (await client.get("/api/v1/directory", params={"q": stored.employee_code})).json()["items"]
        assert found == []


async def test_a_session_of_one_organization_is_refused_by_another_organizations_records(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """The organization comes from the session, never from the request: no header, parameter or
    body field selects one."""
    a, b = tenants
    async with b.api.client() as client:
        await signed_in(b.api, client, b.admin)
        for attempt in (
            {"headers": {"X-Organization-Id": str(a.api.organization_id)}},
            {"params": {"organization_id": str(a.api.organization_id)}},
        ):
            response = await client.get(f"/api/v1/employees/{a.employee_id}", **attempt)  # type: ignore[arg-type]
            assert response.status_code == 404
