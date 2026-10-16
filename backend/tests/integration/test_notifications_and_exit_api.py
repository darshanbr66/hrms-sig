"""In-app notifications and preferences (NTF-1 to NTF-3), the one-step exit (MVP item 11) and
emergency contacts (EMP-3). All data is obviously fake."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.modules.notify.models import EmailOutbox
from app.platform.authz.roles import RoleKey
from tests.api_support import Api
from tests.people_data import add_job, create_employee, create_org_units, fake_code


async def employee_with_manager(api: Api) -> tuple[uuid.UUID, uuid.UUID]:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        manager = await create_employee(session, joined=today - timedelta(days=100))
        await add_job(session, units, manager, manager_id=None, effective_from=today - timedelta(days=100))
        employee = await create_employee(session, joined=today - timedelta(days=100))
        await add_job(
            session, units, employee, manager_id=manager, effective_from=today - timedelta(days=100)
        )
    return manager, employee


async def leave_type_and_policy(api: Api, location_id: uuid.UUID | None = None) -> str:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, admin)
        created = await client.post(
            "/api/v1/leave-types",
            json={
                "code": fake_code("LT"),
                "name": "Test notified leave",
                "is_paid": True,
                "requires_approval": True,
                "allows_half_day": False,
                "counts_weekly_offs": True,
                "counts_holidays": True,
            },
        )
        leave_type_id = created.json()["id"]
        policy = await client.post(
            "/api/v1/leave-policies",
            json={
                "leave_type_id": leave_type_id,
                "effective_from": "2020-01-01",
                "leave_year_start_month": 1,
                "leave_year_start_day": 1,
                "accrual_method": "none",
                "allow_negative_balance": True,
                "max_negative_days": "10",
                **({"location_id": str(location_id)} if location_id else {}),
            },
        )
        assert policy.status_code == 201, policy.text
    return str(leave_type_id)


async def test_notifications_follow_the_recipients_preferences(api: Api) -> None:
    manager_id, employee_id = await employee_with_manager(api)
    manager = await api.create_account(employee_id=manager_id)
    employee = await api.create_account(employee_id=employee_id)
    [(location_id,)] = await api.fetch(
        "SELECT location_id FROM people.employee_jobs WHERE employee_id = :id", id=employee_id
    )
    leave_type_id = await leave_type_and_policy(api, location_id)
    start = api.clock.now().date() + timedelta(days=30)

    async def request_leave(offset: int) -> None:
        async with api.client() as client:
            await api.sign_in(client, employee)
            day = (start + timedelta(days=offset)).isoformat()
            made = await client.post(
                "/api/v1/me/leave-requests",
                json={
                    "leave_type_id": leave_type_id,
                    "start_date": day,
                    "end_date": day,
                    "reason": "Test: private",
                },
                headers={"Idempotency-Key": f"test-{uuid.uuid4()}"},
            )
            assert made.status_code == 201, made.text

    await request_leave(0)
    async with api.client() as client:
        await api.sign_in(client, manager)
        inbox = (await client.get("/api/v1/me/notifications")).json()
        [note] = [n for n in inbox["items"] if n["type"] == "leave.approval_requested"]
        assert note["read"] is False
        assert "Test: private" not in (note["body"] or "")
        assert inbox["unread_count"] >= 1
        assert (await client.post(f"/api/v1/me/notifications/{note['id']}/read")).json()["read"] is True
        # Muting both channels stops both.
        muted = await client.put(
            "/api/v1/me/notification-preferences",
            json={
                "changes": [
                    {"type": "leave.approval_requested", "channel": "in_app", "enabled": False},
                    {"type": "leave.approval_requested", "channel": "email", "enabled": False},
                ]
            },
        )
        assert muted.status_code == 200
        unavailable = await client.put(
            "/api/v1/me/notification-preferences",
            json={"changes": [{"type": "export.ready", "channel": "email", "enabled": False}]},
        )
        assert unavailable.status_code == 422
    async with api.database.unit_of_work() as session:
        before = len(
            list(await session.scalars(select(EmailOutbox).where(EmailOutbox.user_id == manager.user_id)))
        )
    await request_leave(2)
    async with api.database.unit_of_work() as session:
        after = len(
            list(await session.scalars(select(EmailOutbox).where(EmailOutbox.user_id == manager.user_id)))
        )
    assert after == before
    async with api.client() as client:
        await api.sign_in(client, manager)
        approvals = [
            n
            for n in (await client.get("/api/v1/me/notifications")).json()["items"]
            if n["type"] == "leave.approval_requested"
        ]
        assert len(approvals) == 1
    # Nobody reads or changes someone else's notification.
    async with api.client() as client:
        await api.sign_in(client, employee)
        assert (await client.post(f"/api/v1/me/notifications/{note['id']}/read")).status_code == 404
        assert (await client.delete(f"/api/v1/me/notifications/{note['id']}")).status_code == 404


async def test_exit_disables_the_account_and_revokes_every_session_in_one_step(api: Api) -> None:
    _, employee_id = await employee_with_manager(api)
    employee = await api.create_account(employee_id=employee_id)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as leaver:
        await api.sign_in(leaver, employee)
        assert (await leaver.get("/api/v1/me")).status_code == 200
        async with api.client() as client:
            await api.sign_in(client, hr)
            body = {"last_working_day": api.clock.now().date().isoformat(), "reason": "Test: resigned"}
            needs_step_up = await client.post(f"/api/v1/employees/{employee_id}/exit", json=body)
            assert needs_step_up.status_code == 403
            assert needs_step_up.json()["type"] == "/problems/step-up-required"
            await api.step_up(client, hr)
            done = await client.post(f"/api/v1/employees/{employee_id}/exit", json=body)
            assert done.status_code == 201, done.text
            assert done.json()["account"] == "disabled"
            assert done.json()["sessions_revoked"] >= 1
            assert done.json()["status_change"]["to_status"] == "exited"
        assert (await leaver.get("/api/v1/me")).status_code == 401
    [(status,)] = await api.fetch("SELECT status FROM identity.users WHERE id = :id", id=employee.user_id)
    assert status == "disabled"
    actions = {
        row[0]
        for row in await api.fetch(
            "SELECT action FROM audit.audit_log WHERE subject_employee_id = :id", id=employee_id
        )
    }
    assert {"employee.status.changed", "identity.user.disabled"} <= actions


async def test_an_administrators_account_is_left_for_a_system_admin(api: Api) -> None:
    _, employee_id = await employee_with_manager(api)
    admin_account = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,), employee_id=employee_id)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, hr)
        await api.step_up(client, hr)
        done = await client.post(
            f"/api/v1/employees/{employee_id}/exit",
            json={"last_working_day": api.clock.now().date().isoformat(), "reason": "Test: left"},
        )
        assert done.json()["account"] == "administrator_left_active"
    [(status,)] = await api.fetch(
        "SELECT status FROM identity.users WHERE id = :id", id=admin_account.user_id
    )
    assert status == "active"


async def test_emergency_contacts_are_personal_and_audited(api: Api) -> None:
    manager_id, employee_id = await employee_with_manager(api)
    employee = await api.create_account(employee_id=employee_id)
    manager = await api.create_account(employee_id=manager_id)
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    contacts = {
        "contacts": [{"name": "Test Contact", "relationship": "Test sibling", "phone": "+91 00000 00000"}]
    }
    async with api.client() as client:
        await api.sign_in(client, employee)
        assert (
            await client.put(
                "/api/v1/me/emergency-contacts",
                json={"contacts": [{"name": "X", "relationship": "Y", "phone": "call me"}]},
            )
        ).status_code == 422
        saved = await client.put("/api/v1/me/emergency-contacts", json=contacts)
        assert saved.status_code == 200
        assert (await client.get("/api/v1/me/emergency-contacts")).json()["items"][0]["priority"] == 1
    async with api.client() as client:
        await api.sign_in(client, manager)
        # Not granted to managers by default: the record is hidden.
        assert (await client.get(f"/api/v1/employees/{employee_id}/emergency-contacts")).status_code == 404
    async with api.client() as client:
        await api.sign_in(client, hr)
        read = await client.get(f"/api/v1/employees/{employee_id}/emergency-contacts")
        assert read.json()["items"][0]["name"] == "Test Contact"
    rows = await api.fetch(
        "SELECT action, changes::text FROM audit.audit_log WHERE subject_employee_id = :id "
        "AND action LIKE 'employee.emergency_contacts.%'",
        id=employee_id,
    )
    assert {row[0] for row in rows} == {
        "employee.emergency_contacts.updated",
        "employee.emergency_contacts.read",
    }
    assert all("Test Contact" not in (row[1] or "") for row in rows)


async def test_saved_preferences_are_read_back_and_turned_on_again(api: Api) -> None:
    """What the web app shows after a reload is what was saved, in both directions."""
    _, employee = await employee_with_manager(api)
    account = await api.create_account(employee_id=employee)

    def channels(body: dict[str, list[dict[str, object]]], kind: str) -> object:
        return next(item["channels"] for item in body["items"] if item["type"] == kind)

    async with api.client() as client:
        await api.sign_in(client, account)
        initial = (await client.get("/api/v1/me/notification-preferences")).json()
        assert channels(initial, "leave.request_updated") == {"in_app": True, "email": True}
        off = await client.put(
            "/api/v1/me/notification-preferences",
            json={"changes": [{"type": "leave.request_updated", "channel": "email", "enabled": False}]},
        )
        assert off.status_code == 200
        assert channels(off.json(), "leave.request_updated") == {"in_app": True, "email": False}

    async with api.client() as client:
        await api.sign_in(client, account)
        reread = (await client.get("/api/v1/me/notification-preferences")).json()
        assert channels(reread, "leave.request_updated") == {"in_app": True, "email": False}
        on = await client.put(
            "/api/v1/me/notification-preferences",
            json={"changes": [{"type": "leave.request_updated", "channel": "email", "enabled": True}]},
        )
        assert channels(on.json(), "leave.request_updated") == {"in_app": True, "email": True}
        again = (await client.get("/api/v1/me/notification-preferences")).json()
        assert channels(again, "leave.request_updated") == {"in_app": True, "email": True}
