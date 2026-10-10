"""Looking up accounts as an administrator (`GET /users/{id}`, filters on `GET /users`)."""

import uuid
from datetime import timedelta

from app.platform.authz.roles import RoleKey
from tests.api_support import Api
from tests.people_data import add_job, create_employee, create_org_units


async def test_an_administrator_reads_one_account_and_filters_the_list(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=today - timedelta(days=5))
        await add_job(session, units, employee_id, manager_id=None, effective_from=today - timedelta(days=5))
    linked = await api.create_account(employee_id=employee_id)
    async with api.client() as client:
        await api.sign_in(client, admin)
        one = await client.get(f"/api/v1/users/{linked.user_id}")
        assert one.status_code == 200
        assert one.json()["email"] == linked.email
        assert one.json()["employee_id"] == str(employee_id)
        assert (await client.get(f"/api/v1/users/{uuid.uuid4()}")).status_code == 404

        by_employee = await client.get("/api/v1/users", params={"employee_id": str(employee_id)})
        assert [item["id"] for item in by_employee.json()["items"]] == [str(linked.user_id)]
        by_email = await client.get("/api/v1/users", params={"q": linked.email.split("@")[0]})
        assert [item["id"] for item in by_email.json()["items"]] == [str(linked.user_id)]
        wildcard = await client.get("/api/v1/users", params={"q": "%"})
        assert wildcard.json()["items"] == []
