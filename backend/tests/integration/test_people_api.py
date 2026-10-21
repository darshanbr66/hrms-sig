"""Organization and employee records over HTTP (docs/api-architecture.md §8, authorization-model §3.2).

All data is obviously fake (docs/database-design.md §10).
"""

import json
import uuid
from datetime import timedelta
from typing import Any

from app.modules.people.models import Employee, EmployeeStatus
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.roles import RoleKey
from tests.api_support import Account, Api
from tests.people_data import OrgUnits, add_job, create_employee, create_org_units, fake_code


def employee_body(
    units: OrgUnits, *, joined: str, manager: uuid.UUID | None = None, **extra: Any
) -> dict[str, Any]:
    code = fake_code()
    return {
        "employee_code": code,
        "legal_first_name": "Test",
        "legal_last_name": code,
        "work_email": f"{code.lower()}@dev.example",
        "date_of_joining": joined,
        "job": {
            "department_id": str(units.department_id),
            "designation_id": str(units.designation_id),
            "location_id": str(units.location_id),
            "manager_employee_id": str(manager) if manager else None,
            "employment_type": "full_time",
        },
        **extra,
    }


async def units(api: Api) -> OrgUnits:
    async with api.database.unit_of_work() as session:
        return await create_org_units(session)


async def employee_account(api: Api, units_: OrgUnits, *, manager: uuid.UUID | None = None) -> Account:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        employee = await create_employee(session, joined=today - timedelta(days=400))
        await add_job(
            session, units_, employee, manager_id=manager, effective_from=today - timedelta(days=400)
        )
    return await api.create_account(employee_id=employee)


# --- organization -------------------------------------------------------------------------------


async def test_hr_admin_maintains_locations_with_versions_and_audit(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    code = fake_code()
    async with api.client() as client:
        await api.sign_in(client, admin)
        bad = await client.post(
            "/api/v1/locations",
            json={"code": code, "name": "Test office", "time_zone": "Mars/Base", "country_code": "IN"},
        )
        assert bad.status_code == 422
        assert bad.json()["errors"][0]["field"] == "time_zone"

        created = await client.post(
            "/api/v1/locations",
            json={"code": code, "name": "Test office", "time_zone": "Asia/Kolkata", "country_code": "IN"},
        )
        assert created.status_code == 201, created.text
        location = created.json()
        assert created.headers["ETag"] == '"1"'

        duplicate = await client.post(
            "/api/v1/locations",
            json={"code": code.lower(), "name": "Other", "time_zone": "Asia/Kolkata", "country_code": "IN"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "org.code_in_use"

        path = f"/api/v1/locations/{location['id']}"
        assert (await client.patch(path, json={"name": "Renamed"})).status_code == 422  # no If-Match
        renamed = await client.patch(path, json={"name": "Renamed"}, headers={"If-Match": '"1"'})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["version"] == 2
        stale = await client.patch(path, json={"name": "Again"}, headers={"If-Match": '"1"'})
        assert stale.status_code == 412

        archived = await client.patch(path, json={"status": "archived"}, headers={"If-Match": '"2"'})
        assert archived.json()["status"] == "archived"
        listed = (await client.get("/api/v1/locations")).json()["items"]
        assert location["id"] not in {item["id"] for item in listed}
        everything = (await client.get("/api/v1/locations", params={"include_archived": "true"})).json()[
            "items"
        ]
        assert location["id"] in {item["id"] for item in everything}

    actions = [
        (row.action, (row.changes or {}).get("name"))
        for row in await api.records(
            AuditLogRecord, {"target_id": location["id"]}, sort=[("recorded_at", 1), ("id", 1)]
        )
    ]
    assert [row[0] for row in actions] == [
        "org.location.created",
        "org.location.updated",
        "org.location.updated",
    ]
    assert actions[1][1] == {"old": "Test office", "new": "Renamed"}


async def test_a_department_cannot_sit_inside_itself(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, admin)
        parent = (
            await client.post("/api/v1/departments", json={"code": fake_code(), "name": "Parent"})
        ).json()
        child = (
            await client.post(
                "/api/v1/departments", json={"code": fake_code(), "name": "Child", "parent_id": parent["id"]}
            )
        ).json()
        cycle = await client.patch(
            f"/api/v1/departments/{parent['id']}",
            json={"parent_id": child["id"]},
            headers={"If-Match": '"1"'},
        )
        assert cycle.status_code == 422
        assert cycle.json()["errors"][0]["code"] == "department.cycle"


async def test_employees_read_the_organization_but_cannot_change_it(api: Api) -> None:
    account = await employee_account(api, await units(api))
    async with api.client() as client:
        await api.sign_in(client, account)
        assert (await client.get("/api/v1/departments")).status_code == 200
        refused = await client.post("/api/v1/departments", json={"code": fake_code(), "name": "Nope"})
        assert refused.status_code == 403


# --- employee records ---------------------------------------------------------------------------


async def test_hr_creates_an_employee_with_a_first_job_and_an_audit_record(api: Api) -> None:
    org_units = await units(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    today = api.clock.now().date()
    async with api.client() as client:
        await api.sign_in(client, hr)
        created = await client.post(
            "/api/v1/employees", json=employee_body(org_units, joined=today.isoformat())
        )
        assert created.status_code == 201, created.text
        employee = created.json()
        assert employee["status"] == "active"
        assert employee["job"]["department"]["id"] == str(org_units.department_id)
        assert employee["job"]["department"]["name"] == "Test department"

        future = await client.post(
            "/api/v1/employees",
            json=employee_body(org_units, joined=(today + timedelta(days=10)).isoformat()),
        )
        assert future.json()["status"] == "pre_joining"

        duplicate = employee_body(org_units, joined=today.isoformat())
        duplicate["employee_code"] = employee["employee_code"]
        clash = await client.post("/api/v1/employees", json=duplicate)
        assert clash.status_code == 422
        assert clash.json()["errors"][0]["field"] == "employee_code"

        jobs = (await client.get(f"/api/v1/employees/{employee['id']}/jobs")).json()["items"]
        assert [job["change_reason"] for job in jobs] == ["joining"]

        stats = (await client.get("/api/v1/employees/statistics")).json()
        assert stats["employed"] >= 1
        assert any(item["employee"]["id"] == future.json()["id"] for item in stats["starting_soon"])

    [audited] = await api.records(
        AuditLogRecord, {"subject_employee_id": uuid.UUID(employee["id"]), "action": "employee.created"}
    )
    permission, changes = audited.permission_used, audited.changes or {}
    assert permission == "employee.create"
    assert changes["employee_code"] == {"redacted": True}
    assert changes["legal_first_name"] == {"old": None, "new": "Test"}


async def test_records_are_read_only_within_scope(api: Api) -> None:
    org_units = await units(api)
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        manager_id = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(
            session, org_units, manager_id, manager_id=None, effective_from=today - timedelta(days=30)
        )
        report_id = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(
            session, org_units, report_id, manager_id=manager_id, effective_from=today - timedelta(days=30)
        )
        stranger_id = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(
            session, org_units, stranger_id, manager_id=None, effective_from=today - timedelta(days=30)
        )
    manager = await api.create_account(employee_id=manager_id)
    report = await api.create_account(employee_id=report_id)

    async with api.client() as client:
        await api.sign_in(client, manager)
        assert (await client.get(f"/api/v1/employees/{report_id}")).status_code == 200
        assert (await client.get(f"/api/v1/employees/{stranger_id}")).status_code == 404
        listed = {item["id"] for item in (await client.get("/api/v1/employees")).json()["items"]}
        assert listed == {str(manager_id), str(report_id)}
        # Managers see job history without HR notes, and never personal details.
        assert (await client.get(f"/api/v1/employees/{report_id}/personal")).status_code == 404

    async with api.client() as client:
        await api.sign_in(client, report)
        assert (await client.get(f"/api/v1/employees/{manager_id}")).status_code == 404
        listed = {item["id"] for item in (await client.get("/api/v1/employees")).json()["items"]}
        assert listed == {str(report_id)}
        # The directory shows colleagues with directory fields only.
        entries = (await client.get("/api/v1/directory", params={"limit": 100})).json()["items"]
        assert set(entries[0]) == {
            "id",
            "name",
            "preferred_name",
            "work_email",
            "department",
            "designation",
            "location",
        }
        own = await client.get("/api/v1/me/profile")
        assert own.status_code == 200
        assert own.json()["job"]["manager"]["id"] == str(manager_id)


async def test_directory_search_finds_people_by_name_and_code(api: Api) -> None:
    org_units = await units(api)
    reader = await employee_account(api, org_units)
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        target = await create_employee(session, joined=today - timedelta(days=5))
        await add_job(session, org_units, target, manager_id=None, effective_from=today - timedelta(days=5))
    [stored] = await api.records(Employee, {"id": target})
    code = stored.employee_code
    async with api.client() as client:
        await api.sign_in(client, reader)
        found = (await client.get("/api/v1/directory", params={"q": code.lower()})).json()["items"]
        assert [entry["id"] for entry in found] == [str(target)]
        assert (await client.get("/api/v1/directory", params={"q": "100%_"})).json()["items"] == []


async def test_job_changes_close_the_current_row_and_refuse_reporting_cycles(api: Api) -> None:
    org_units = await units(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        boss = await create_employee(session, joined=today - timedelta(days=100))
        await add_job(session, org_units, boss, manager_id=None, effective_from=today - timedelta(days=100))
        worker = await create_employee(session, joined=today - timedelta(days=100))
        await add_job(session, org_units, worker, manager_id=boss, effective_from=today - timedelta(days=100))
    change = {
        "effective_from": today.isoformat(),
        "department_id": str(org_units.department_id),
        "designation_id": str(org_units.designation_id),
        "location_id": str(org_units.location_id),
        "manager_employee_id": str(worker),
        "employment_type": "full_time",
        "change_reason": "manager_change",
    }
    async with api.client() as client:
        await api.sign_in(client, hr)
        cycle = await client.post(f"/api/v1/employees/{boss}/jobs", json=change)
        assert cycle.status_code == 422
        assert cycle.json()["errors"][0]["code"] == "job.manager_cycle"

        promoted = await client.post(
            f"/api/v1/employees/{worker}/jobs",
            json={**change, "manager_employee_id": None, "change_reason": "promotion", "notes": "Test note"},
        )
        assert promoted.status_code == 201, promoted.text
        history = promoted.json()["items"]
        assert [job["change_reason"] for job in history] == ["promotion", "joining"]
        assert history[1]["effective_to"] == today.isoformat()
        assert history[0]["notes"] == "Test note"

        too_early = await client.post(
            f"/api/v1/employees/{worker}/jobs", json={**change, "manager_employee_id": None}
        )
        assert too_early.status_code == 422


async def test_status_changes_are_recorded_and_exit_dates_checked(api: Api) -> None:
    org_units = await units(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        employee = await create_employee(session, joined=today - timedelta(days=100))
        await add_job(
            session, org_units, employee, manager_id=None, effective_from=today - timedelta(days=100)
        )
    path = f"/api/v1/employees/{employee}/status-changes"
    async with api.client() as client:
        await api.sign_in(client, hr)
        future_exit = await client.post(
            path,
            json={
                "to_status": "exited",
                "effective_date": (today + timedelta(days=5)).isoformat(),
                "reason": "Test",
            },
        )
        assert future_exit.status_code == 422
        notice = await client.post(
            path,
            json={
                "to_status": "on_notice",
                "effective_date": today.isoformat(),
                "reason": "Test resignation",
            },
        )
        assert notice.status_code == 201, notice.text
        exited = await client.post(
            path, json={"to_status": "exited", "effective_date": today.isoformat(), "reason": "Test last day"}
        )
        assert exited.status_code == 201
        again = await client.post(
            path, json={"to_status": "active", "effective_date": today.isoformat(), "reason": "Test"}
        )
        assert again.status_code == 409
        history = (await client.get(path)).json()["items"]
        assert [(item["from_status"], item["to_status"]) for item in history] == [
            ("on_notice", "exited"),
            ("active", "on_notice"),
        ]
        record = (await client.get(f"/api/v1/employees/{employee}")).json()
        assert record["status"] == "exited"
        assert record["date_of_exit"] == today.isoformat()


async def test_a_restricted_hr_assignment_creates_employees_only_in_its_department(api: Api) -> None:
    mine, other = await units(api), await units(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,), role_scope={"department_id": mine.department_id})
    today = api.clock.now().date().isoformat()
    async with api.client() as client:
        await api.sign_in(client, hr)
        refused = await client.post("/api/v1/employees", json=employee_body(other, joined=today))
        assert refused.status_code == 403
        allowed = await client.post("/api/v1/employees", json=employee_body(mine, joined=today))
        assert allowed.status_code == 201, allowed.text


async def test_personal_details_are_separate_audited_and_never_copied_to_audit(api: Api) -> None:
    org_units = await units(api)
    account = await employee_account(api, org_units)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, account)
        empty = await client.get("/api/v1/me/personal")
        assert empty.json()["version"] == 0
        saved = await client.patch(
            "/api/v1/me/personal",
            json={
                "personal_phone": "+91 00000 00000",
                "current_address": {"line1": "1 Test Street", "city": "Test City", "country_code": "IN"},
            },
            headers={"If-Match": '"0"'},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["current_address"]["city"] == "Test City"
        preferred = await client.patch(
            "/api/v1/me/profile", json={"preferred_name": "Testy"}, headers={"If-Match": '"1"'}
        )
        assert preferred.status_code == 200, preferred.text
        assert preferred.json()["preferred_name"] == "Testy"
        refused = await client.patch(
            "/api/v1/me/profile", json={"legal_first_name": "Changed"}, headers={"If-Match": '"2"'}
        )
        assert refused.status_code == 422

    async with api.client() as client:
        await api.sign_in(client, hr)
        read = await client.get(f"/api/v1/employees/{account.employee_id}/personal")
        assert read.status_code == 200
        assert read.json()["personal_phone"] == "+91 00000 00000"

    rows = [
        (row.action, json.dumps(row.changes))
        for row in await api.records(
            AuditLogRecord,
            {"subject_employee_id": account.employee_id, "action": {"$regex": "^employee[.]personal[.]"}},
            sort=[("recorded_at", 1), ("id", 1)],
        )
    ]
    assert [row[0] for row in rows] == ["employee.personal.updated", "employee.personal.read"]
    assert "Test Street" not in rows[0][1]
    assert "00000" not in rows[0][1]


# --- organization management (M2) ---------------------------------------------------------------


async def test_a_unit_in_use_cannot_be_archived_until_its_people_move(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    org_units = await units(api)
    spare = await units(api)
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        employee = await create_employee(session, joined=today - timedelta(days=50))
        await add_job(
            session, org_units, employee, manager_id=None, effective_from=today - timedelta(days=50)
        )
    async with api.client() as client:
        await api.sign_in(client, admin)
        for kind, unit_id in (
            ("departments", org_units.department_id),
            ("designations", org_units.designation_id),
            ("locations", org_units.location_id),
        ):
            refused = await client.patch(
                f"/api/v1/{kind}/{unit_id}", json={"status": "archived"}, headers={"If-Match": '"1"'}
            )
            assert refused.status_code == 409, (kind, refused.text)
            assert refused.json()["code"] == "org.in_use"
            assert "1 employee is still placed" in refused.json()["detail"]

        # Once the employee moves, the old units are free to archive.
        moved = await client.post(
            f"/api/v1/employees/{employee}/jobs",
            json={
                "effective_from": today.isoformat(),
                "department_id": str(spare.department_id),
                "designation_id": str(spare.designation_id),
                "location_id": str(spare.location_id),
                "employment_type": "full_time",
                "change_reason": "transfer",
            },
        )
        assert moved.status_code == 201, moved.text
        archived = await client.patch(
            f"/api/v1/departments/{org_units.department_id}",
            json={"status": "archived"},
            headers={"If-Match": '"1"'},
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"

        # A future-dated placement also counts.
        later = await units(api)
        future = await client.post(
            "/api/v1/employees",
            json=employee_body(later, joined=(today + timedelta(days=20)).isoformat()),
        )
        assert future.status_code == 201
        busy = await client.patch(
            f"/api/v1/locations/{later.location_id}", json={"status": "archived"}, headers={"If-Match": '"1"'}
        )
        assert busy.status_code == 409


async def test_department_descriptions_parents_and_children_are_checked(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, admin)
        parent = await client.post(
            "/api/v1/departments",
            json={"code": fake_code(), "name": "Test parent", "description": "  Runs the tests.  "},
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["id"]
        assert parent.json()["description"] == "Runs the tests."
        too_long = await client.post(
            "/api/v1/departments", json={"code": fake_code(), "name": "Long", "description": "x" * 1001}
        )
        assert too_long.status_code == 422
        child = await client.post(
            "/api/v1/departments", json={"code": fake_code(), "name": "Test child", "parent_id": parent_id}
        )
        child_id = child.json()["id"]

        busy = await client.patch(
            f"/api/v1/departments/{parent_id}", json={"status": "archived"}, headers={"If-Match": '"1"'}
        )
        assert busy.status_code == 409
        assert busy.json()["code"] == "department.has_active_children"

        archived_child = await client.patch(
            f"/api/v1/departments/{child_id}", json={"status": "archived"}, headers={"If-Match": '"1"'}
        )
        assert archived_child.status_code == 200
        under_archived = await client.post(
            "/api/v1/departments", json={"code": fake_code(), "name": "Orphan", "parent_id": child_id}
        )
        assert under_archived.status_code == 422
        assert under_archived.json()["errors"][0]["code"] == "department.parent_archived"

        cleared = await client.patch(
            f"/api/v1/departments/{parent_id}", json={"description": None}, headers={"If-Match": '"1"'}
        )
        assert cleared.status_code == 200
        assert cleared.json()["description"] is None

    [updated] = await api.records(
        AuditLogRecord, {"target_id": str(parent_id), "action": "org.department.updated"}
    )
    changes = updated.changes or {}
    assert changes["description"] == {"old": "Runs the tests.", "new": None}


async def test_the_employee_list_filters_sorts_pages_and_rejects_bad_parameters(api: Api) -> None:
    org_units = await units(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    today = api.clock.now().date()
    joined = [today - timedelta(days=days) for days in (300, 10, 120)]
    ids = []
    async with api.database.unit_of_work() as session:
        for date_of_joining in joined:
            employee = await create_employee(session, joined=date_of_joining)
            await add_job(session, org_units, employee, manager_id=None, effective_from=date_of_joining)
            ids.append(str(employee))
    newest_first = [ids[1], ids[2], ids[0]]
    async with api.client() as client:
        await api.sign_in(client, hr)
        listed = await client.get(
            "/api/v1/employees",
            params={"designation_id": str(org_units.designation_id), "sort": "joined_newest", "limit": 100},
        )
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == newest_first

        paged: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {
                "location_id": str(org_units.location_id),
                "sort": "joined_newest",
                "limit": 1,
            }
            if cursor:
                params["cursor"] = cursor
            page = (await client.get("/api/v1/employees", params=params)).json()
            paged += [item["id"] for item in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert paged == newest_first

        oldest = await client.get(
            "/api/v1/employees",
            params={"department_id": str(org_units.department_id), "sort": "joined_oldest"},
        )
        assert [item["id"] for item in oldest.json()["items"]] == list(reversed(newest_first))
        by_code = await client.get(
            "/api/v1/employees", params={"department_id": str(org_units.department_id), "sort": "code"}
        )
        codes = [item["employee_code"] for item in by_code.json()["items"]]
        assert codes == sorted(codes, key=str.lower)

        for bad in (
            {"sort": "salary"},
            {"limit": 0},
            {"limit": 101},
            {"department_id": "not-an-id"},
            {"status": "retired"},
            {"q": "x" * 101},
        ):
            assert (await client.get("/api/v1/employees", params=bad)).status_code == 422, bad


async def test_headcounts_by_unit_stay_within_the_readers_scope(api: Api) -> None:
    org_units = await units(api)
    other = await units(api)
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        manager = await create_employee(session, joined=today - timedelta(days=200))
        await add_job(
            session, org_units, manager, manager_id=None, effective_from=today - timedelta(days=200)
        )
        report = await create_employee(session, joined=today - timedelta(days=3))
        await add_job(
            session, org_units, report, manager_id=manager, effective_from=today - timedelta(days=3)
        )
        stranger = await create_employee(session, joined=today - timedelta(days=200))
        await add_job(session, other, stranger, manager_id=None, effective_from=today - timedelta(days=200))
    manager_account = await api.create_account(employee_id=manager)
    hr = await api.create_account(roles=(RoleKey.HR.value,))

    async with api.client() as client:
        await api.sign_in(client, manager_account)
        stats = (await client.get("/api/v1/employees/statistics")).json()
        assert stats["by_department"] == [
            {"id": str(org_units.department_id), "name": "Test department", "employed": 2}
        ]
        assert {item["id"] for item in stats["by_location"]} == {str(org_units.location_id)}
        assert [item["employee"]["id"] for item in stats["recent_joiners"]] == [str(report)]

    async with api.client() as client:
        await api.sign_in(client, hr)
        stats = (await client.get("/api/v1/employees/statistics")).json()
        departments = {item["id"]: item["employed"] for item in stats["by_department"]}
        assert departments[str(org_units.department_id)] == 2
        assert departments[str(other.department_id)] == 1
        counts = [item["employed"] for item in stats["by_department"]]
        assert counts == sorted(counts, reverse=True)


# --- department heads -----------------------------------------------------------------------------


async def test_a_department_head_must_be_a_current_or_future_employee(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    org_units = await units(api)
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        head = await create_employee(session, joined=today - timedelta(days=100))
        await add_job(session, org_units, head, manager_id=None, effective_from=today - timedelta(days=100))
        left = await create_employee(
            session, joined=today - timedelta(days=300), exited=today - timedelta(days=1)
        )
        leaving_today = await create_employee(session, joined=today - timedelta(days=300), exited=today)
    [stored_head] = await api.records(Employee, {"id": head})
    head_code = stored_head.employee_code
    async with api.client() as client:
        await api.sign_in(client, admin)
        created = await client.post(
            "/api/v1/departments",
            json={"code": fake_code(), "name": "Test headed", "head_employee_id": str(head)},
        )
        assert created.status_code == 201, created.text
        department = created.json()
        assert department["head_employee_id"] == str(head)
        assert department["head_name"] == f"Test {head_code}"
        listed = (await client.get("/api/v1/departments")).json()["items"]
        assert next(d for d in listed if d["id"] == department["id"])["head_name"] == f"Test {head_code}"

        path = f"/api/v1/departments/{department['id']}"
        unknown = await client.patch(
            path, json={"head_employee_id": str(uuid.uuid4())}, headers={"If-Match": '"1"'}
        )
        assert unknown.status_code == 422
        assert unknown.json()["errors"][0]["code"] == "org.unknown_employee"
        gone = await client.patch(path, json={"head_employee_id": str(left)}, headers={"If-Match": '"1"'})
        assert gone.status_code == 422
        assert gone.json()["errors"][0]["code"] == "department.head_left"
        bad_create = await client.post(
            "/api/v1/departments", json={"code": fake_code(), "name": "Bad", "head_employee_id": str(left)}
        )
        assert bad_create.status_code == 422
        # The last working day is still a working day.
        last_day = await client.patch(
            path, json={"head_employee_id": str(leaving_today)}, headers={"If-Match": '"1"'}
        )
        assert last_day.status_code == 200, last_day.text
        cleared = await client.patch(path, json={"head_employee_id": None}, headers={"If-Match": '"2"'})
        assert cleared.json()["head_name"] is None

    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, hr)
        refused = await client.patch(
            f"/api/v1/departments/{department['id']}",
            json={"head_employee_id": str(head)},
            headers={"If-Match": '"3"'},
        )
        assert refused.status_code == 403


# --- employment definitions (EMP-7) -------------------------------------------------------------


async def test_current_future_and_former_follow_the_dates_and_match_the_counts(api: Api) -> None:
    org_units = await units(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        joined_today = await create_employee(session, joined=today)
        await add_job(session, org_units, joined_today, manager_id=None, effective_from=today)
        long_ago = await create_employee(session, joined=today - timedelta(days=400))
        await add_job(
            session, org_units, long_ago, manager_id=None, effective_from=today - timedelta(days=400)
        )
        last_day_today = await create_employee(session, joined=today - timedelta(days=400), exited=today)
        await add_job(
            session, org_units, last_day_today, manager_id=None, effective_from=today - timedelta(days=400)
        )
        left_yesterday = await create_employee(
            session, joined=today - timedelta(days=400), exited=today - timedelta(days=1)
        )
        await add_job(
            session,
            org_units,
            left_yesterday,
            manager_id=None,
            effective_from=today - timedelta(days=400),
            effective_to=today,
        )
        joins_tomorrow = await create_employee(
            session, joined=today + timedelta(days=1), status=EmployeeStatus.PRE_JOINING
        )
        await add_job(
            session, org_units, joins_tomorrow, manager_id=None, effective_from=today + timedelta(days=1)
        )

    async def listed(client: Any, employment: str) -> set[str]:
        page = await client.get(
            "/api/v1/employees",
            params={"department_id": str(org_units.department_id), "employment": employment, "limit": 100},
        )
        assert page.status_code == 200, page.text
        return {item["id"] for item in page.json()["items"]}

    async with api.client() as client:
        await api.sign_in(client, hr)
        # The department filter uses the job row that applies today, so former and future
        # people are found without it.
        current = await listed(client, "current")
        assert current == {str(joined_today), str(long_ago), str(last_day_today)}
        future = await client.get("/api/v1/employees", params={"employment": "future", "limit": 100})
        assert str(joins_tomorrow) in {item["id"] for item in future.json()["items"]}
        assert str(long_ago) not in {item["id"] for item in future.json()["items"]}
        former = await client.get("/api/v1/employees", params={"employment": "former", "limit": 100})
        former_ids = {item["id"] for item in former.json()["items"]}
        assert str(left_yesterday) in former_ids
        assert str(last_day_today) not in former_ids
        assert (await client.get("/api/v1/employees", params={"employment": "past"})).status_code == 422

        stats = (await client.get("/api/v1/employees/statistics")).json()
        headcount = next(d for d in stats["by_department"] if d["id"] == str(org_units.department_id))
        assert headcount["employed"] == len(current)
        assert str(joined_today) in {item["employee"]["id"] for item in stats["recent_joiners"]}
        assert stats["future_joiners"] >= 1
        assert stats["former"] >= 1


async def test_today_is_the_utc_date_at_the_boundary(api: Api) -> None:
    org_units = await units(api)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    now = api.clock.now()
    api.clock.current = now.replace(hour=23, minute=59, second=0, microsecond=0)
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        starter = await create_employee(
            session, joined=today + timedelta(days=1), status=EmployeeStatus.PRE_JOINING
        )
        await add_job(session, org_units, starter, manager_id=None, effective_from=today + timedelta(days=1))
    params = {"location_id": str(org_units.location_id), "employment": "current"}
    async with api.client() as client:
        await api.sign_in(client, hr)
        before = (await client.get("/api/v1/employees", params=params)).json()["items"]
        assert before == []
        # Two minutes later it is the next UTC date (in India it already was): now current.
        api.clock.advance(timedelta(minutes=2))
        after = (await client.get("/api/v1/employees", params=params)).json()["items"]
        assert [item["id"] for item in after] == [str(starter)]


async def test_a_managers_counts_cover_only_their_current_team(api: Api) -> None:
    org_units = await units(api)
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        manager = await create_employee(session, joined=today - timedelta(days=200))
        await add_job(
            session, org_units, manager, manager_id=None, effective_from=today - timedelta(days=200)
        )
        report = await create_employee(session, joined=today - timedelta(days=50))
        await add_job(
            session, org_units, report, manager_id=manager, effective_from=today - timedelta(days=50)
        )
        future_report = await create_employee(
            session, joined=today + timedelta(days=10), status=EmployeeStatus.PRE_JOINING
        )
        await add_job(
            session, org_units, future_report, manager_id=manager, effective_from=today + timedelta(days=10)
        )
        stranger = await create_employee(session, joined=today - timedelta(days=50))
        await add_job(
            session, org_units, stranger, manager_id=None, effective_from=today - timedelta(days=50)
        )
    account = await api.create_account(employee_id=manager)
    async with api.client() as client:
        await api.sign_in(client, account)
        stats = (await client.get("/api/v1/employees/statistics")).json()
        assert stats["employed"] == 2
        assert stats["future_joiners"] == 0
        assert stats["by_department"][0]["employed"] == 2
        current = (await client.get("/api/v1/employees", params={"employment": "current"})).json()["items"]
        assert {item["id"] for item in current} == {str(manager), str(report)}
        # Someone joining the team later is not part of it yet (authorization-model §7).
        assert (await client.get(f"/api/v1/employees/{future_report}")).status_code == 404
