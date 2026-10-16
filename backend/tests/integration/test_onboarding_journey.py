"""The first-employee journey over HTTP, end to end (docs/module-acceptance.md, organization and
employee records): an HR admin sets up the organization through the API, adds an employee, sends
the linked invite; the employee activates it with an authenticator and then reaches only their own
records.

All data is obviously fake (docs/database-design.md §10).
"""

import uuid
from typing import Any

import httpx
import pyotp

from app.platform.authz.roles import RoleKey
from tests.api_support import PASSWORD, Api, link_token
from tests.people_data import fake_code


async def set_up_organization(client: httpx.AsyncClient) -> dict[str, str]:
    """A location, department and designation, each through its create endpoint."""
    bodies: dict[str, dict[str, Any]] = {
        "locations": {"name": "Test office", "time_zone": "Asia/Kolkata", "country_code": "IN"},
        "departments": {"name": "Test team"},
        "designations": {"name": "Test engineer", "level": 1},
    }
    ids = {}
    for kind, body in bodies.items():
        created = await client.post(f"/api/v1/{kind}", json={"code": fake_code(), **body})
        assert created.status_code == 201, created.text
        ids[kind] = created.json()["id"]
        listed = (await client.get(f"/api/v1/{kind}")).json()["items"]
        assert ids[kind] in {item["id"] for item in listed}
    return ids


def employee_body(units: dict[str, str], today: str) -> dict[str, Any]:
    code = fake_code()
    return {
        "employee_code": code,
        "legal_first_name": "Test",
        "legal_last_name": "Starter",
        "work_email": f"{code.lower()}@dev.example",
        "date_of_joining": today,
        "job": {
            "department_id": units["departments"],
            "designation_id": units["designations"],
            "location_id": units["locations"],
            "manager_employee_id": None,
            "employment_type": "full_time",
        },
    }


async def add_employee(client: httpx.AsyncClient, body: dict[str, Any], units: dict[str, str]) -> str:
    created = await client.post("/api/v1/employees", json=body)
    assert created.status_code == 201, created.text
    employee_id: str = created.json()["id"]
    listed = (await client.get("/api/v1/employees", params={"limit": 100})).json()["items"]
    assert employee_id in {item["id"] for item in listed}
    job = (await client.get(f"/api/v1/employees/{employee_id}")).json()["job"]
    assert job["department"]["id"] == units["departments"]
    assert job["designation"]["id"] == units["designations"]
    assert job["location"]["id"] == units["locations"]
    jobs = (await client.get(f"/api/v1/employees/{employee_id}/jobs")).json()["items"]
    assert [item["change_reason"] for item in jobs] == ["joining"]
    return employee_id


async def activate(api: Api, browser: httpx.AsyncClient, token: str) -> None:
    """Password, then an authenticator: there is no way to finish without one."""
    base = f"/api/v1/auth/invite/{token}"
    assert (await browser.get(base)).json()["valid"] is True
    password = await browser.post(f"{base}/password", json={"password": PASSWORD})
    assert password.status_code == 200, password.text
    # The password step hands out an enrolment token, never a session.
    assert (await browser.get("/api/v1/me")).status_code == 401
    enrolment = password.json()["enrolment_token"]
    setup = (await browser.post(f"{base}/mfa/totp/setup", json={"enrolment_token": enrolment})).json()
    activated = await browser.post(
        f"{base}/mfa/totp/confirm",
        json={
            "enrolment_token": enrolment,
            "factor_id": setup["factor_id"],
            "code": pyotp.TOTP(setup["secret"]).at(api.clock.now()),
        },
    )
    assert activated.status_code == 200, activated.text


async def check_employee_reach(browser: httpx.AsyncClient, employee_id: str, body: dict[str, Any]) -> None:
    me = (await browser.get("/api/v1/me")).json()
    assert me["employee_id"] == employee_id
    assert me["roles"] == [RoleKey.EMPLOYEE.value]
    # Their own records.
    profile = await browser.get("/api/v1/me/profile")
    assert profile.status_code == 200
    assert profile.json()["id"] == employee_id
    assert (await browser.get("/api/v1/me/jobs")).status_code == 200
    assert (await browser.get("/api/v1/me/attendance/today")).status_code == 200
    # The organization is readable but not changeable.
    assert (await browser.get("/api/v1/departments")).status_code == 200
    refused = await browser.post("/api/v1/departments", json={"code": fake_code(), "name": "Nope"})
    assert refused.status_code == 403
    # Nobody else's records, and no administration.
    assert (await browser.get(f"/api/v1/employees/{uuid.uuid4()}")).status_code == 404
    listed = (await browser.get("/api/v1/employees")).json()["items"]
    assert {item["id"] for item in listed} == {employee_id}
    assert (await browser.get("/api/v1/users")).status_code == 403
    another = {**body, "employee_code": fake_code(), "work_email": f"{fake_code().lower()}@dev.example"}
    assert (await browser.post("/api/v1/employees", json=another)).status_code == 403


async def test_hr_admin_sets_up_the_organization_and_onboards_the_first_employee(api: Api) -> None:
    hr_admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, hr_admin)
        units = await set_up_organization(client)
        body = employee_body(units, api.clock.now().date().isoformat())
        employee_id = await add_employee(client, body, units)
        # The invite linked to the record goes to the work email.
        invited = await client.post(
            "/api/v1/users", json={"email": body["work_email"], "employee_id": employee_id}
        )
        assert invited.status_code == 201, invited.text
        assert invited.json()["status"] == "invited"

    [message] = [m for m in await api.deliver() if m.to == body["work_email"]]
    async with api.client() as browser:
        await activate(api, browser, link_token(message))
        await check_employee_reach(browser, employee_id, body)

    org_actions = {
        row[0]
        for row in await api.fetch(
            "SELECT action FROM audit.audit_log WHERE target_id IN (:a, :b, :c)",
            a=units["locations"],
            b=units["departments"],
            c=units["designations"],
        )
    }
    assert org_actions == {"org.location.created", "org.department.created", "org.designation.created"}
    [(permission,)] = await api.fetch(
        "SELECT permission_used FROM audit.audit_log "
        "WHERE subject_employee_id = :id AND action = 'employee.created'",
        id=employee_id,
    )
    assert permission == "employee.create"
