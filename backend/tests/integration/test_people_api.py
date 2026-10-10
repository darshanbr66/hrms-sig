"""Organization and employee records over HTTP (docs/api-architecture.md §8, authorization-model §3.2).

All data is obviously fake (docs/database-design.md §10).
"""

import uuid
from datetime import timedelta
from typing import Any

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

    actions = await api.fetch(
        "SELECT action, changes->'name' FROM audit.audit_log WHERE target_id = :id ORDER BY recorded_at, id",
        id=location["id"],
    )
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

    [(permission, changes)] = await api.fetch(
        "SELECT permission_used, changes FROM audit.audit_log "
        "WHERE subject_employee_id = :id AND action = 'employee.created'",
        id=employee["id"],
    )
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
    [(code,)] = await api.fetch("SELECT employee_code FROM people.employees WHERE id = :id", id=target)
    async with api.client() as client:
        await api.sign_in(client, reader)
        found = (await client.get("/api/v1/directory", params={"q": code.lower()})).json()["items"]
        assert [entry["id"] for entry in found] == [str(target)]
        assert (await client.get("/api/v1/directory", params={"q": "100%_"})).json()["items"] == []
    indexes = await api.fetch("SELECT indexname FROM pg_indexes WHERE indexname = 'ix_employees_search'")
    assert indexes == [("ix_employees_search",)]


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

    rows = await api.fetch(
        "SELECT action, changes::text FROM audit.audit_log WHERE subject_employee_id = :id "
        "AND action LIKE 'employee.personal.%' ORDER BY recorded_at, id",
        id=account.employee_id,
    )
    assert [row[0] for row in rows] == ["employee.personal.updated", "employee.personal.read"]
    assert "Test Street" not in rows[0][1]
    assert "00000" not in rows[0][1]
