"""Permanently erasing one person's data (owner decision 2026-10-23; docs/database-design.md
§9.1, authorization-model §4.2): two super admins, typed employee codes and step-up; every kind
of record deleted, anonymized or kept as the report says; one transaction for the records and a
worker step for the files, complete only once no file is left; nothing for another organization.

All data is obviously fake (docs/database-design.md §10).
"""

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.modules.access.models import UserRole, role_id
from app.modules.attendance.models import AttendanceCorrection, AttendanceEvent, EventSource
from app.modules.documents.models import DocumentVersion
from app.modules.erasure import job as erasure_job
from app.modules.erasure.models import ErasureRequest
from app.modules.identity.models import User
from app.modules.leave.models import LeaveRequest
from app.modules.people.models import EmergencyContact, Employee, EmployeeJob, EmployeePersonal
from app.platform.audit.tables import AuditLogRecord
from app.platform.audit.writer import AuditWriter
from app.platform.authz.roles import RoleKey
from app.platform.db import ERASED, GuardedUpdateError, tenant_scope
from app.platform.files import FileStoreError
from app.platform.scanning import Verdict
from tests.api_support import Account, Api, running_api
from tests.conftest import MongoServer
from tests.integration.test_documents_api import category, scan, upload
from tests.people_data import add_job, create_employee, create_org_units

ERASURES = "/api/v1/employee-erasures"
REASON = "Test: erasure authorized by the data protection officer"


@dataclass
class Person:
    employee_id: uuid.UUID
    code: str
    account: Account
    storage_key: str
    document_id: str
    version_id: str
    department_id: uuid.UUID


async def signed_in(api: Api, client: httpx.AsyncClient, account: Account) -> None:
    await api.sign_in(client, account)
    await api.step_up(client, account)


async def a_person_with_data(api: Api, hr: Account, *, exited: bool = True) -> Person:
    """An employee with a record, personal details, contacts, a linked account, job notes,
    attendance with a correction, a leave request, a document with a stored file and the
    head of a department; offboarded two days ago unless `exited` is False."""
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee_id = await create_employee(session, joined=today - timedelta(days=200))
        job_id = await add_job(
            session, units, employee_id, manager_id=None, effective_from=today - timedelta(days=200)
        )
        session.add(
            EmployeePersonal(
                employee_id=employee_id,
                date_of_birth=date(1990, 1, 1),
                personal_email="test.personal@dev.example",
                personal_phone="+91 00000 00000",
            )
        )
        session.add(
            EmergencyContact(
                employee_id=employee_id,
                priority=1,
                name="Test Contact",
                relationship="Test",
                phone="+91 00000 11111",
            )
        )
        session.add(
            LeaveRequest(
                employee_id=employee_id,
                leave_type_id=uuid.uuid4(),
                start_date=today - timedelta(days=40),
                end_date=today - timedelta(days=40),
                days_requested=Decimal("1"),
                reason="Test: a reason that names a family matter",
                status="approved",
                submitted_by=employee_id,
                idempotency_key=f"test-{uuid.uuid4()}",
            )
        )
    assert (
        await api.update("employee_jobs", {"_id": job_id}, {"notes": "Test: an HR note about the person"})
        == 1
    )
    assert (
        await api.update("departments", {"_id": units.department_id}, {"head_employee_id": employee_id}) == 1
    )
    [employee] = await api.records(Employee, {"id": employee_id})
    await api.update("employees", {"_id": employee_id}, {"work_phone": "+91 00000 22222"})
    account = await api.create_account(employee_id=employee_id)
    await api.update("employees", {"_id": employee_id}, {"work_email": account.email})
    day = today - timedelta(days=10)
    async with api.database.unit_of_work() as session:
        session.add(
            AttendanceEvent(
                employee_id=employee_id,
                event_type="CLOCK_IN",
                occurred_at=datetime.combine(day, time(3, 30)).replace(tzinfo=api.clock.now().tzinfo),
                work_date=day,
                source=EventSource.WEB.value,
                recorded_by=account.user_id,
                ip="192.0.2.10",
                idempotency_key=f"test-{uuid.uuid4()}",
            )
        )
    async with api.client() as client:
        await signed_in(api, client, account)  # a session and a recognized browser
    async with api.client() as client:
        await signed_in(api, client, hr)
        corrected = await client.post(
            f"/api/v1/employees/{employee_id}/attendance/corrections",
            json={
                "work_date": day.isoformat(),
                "reason": "Test: gate log shows the departure",
                "items": [
                    {
                        "action": "add",
                        "event_type": "CLOCK_OUT",
                        "proposed_at": datetime.combine(day, time(12, 0))
                        .replace(tzinfo=api.clock.now().tzinfo)
                        .isoformat(),
                    }
                ],
            },
            headers={"Idempotency-Key": f"test-{uuid.uuid4()}"},
        )
        assert corrected.status_code == 201, corrected.text
        internal = await category(client, "internal", visible=True, upload=False)
        uploaded = await upload(
            client, f"/api/v1/employees/{employee_id}/documents", internal, name="test-passport-scan.pdf"
        )
        assert uploaded.status_code == 201, uploaded.text
        document = uploaded.json()
        if exited:
            left = await client.post(
                f"/api/v1/employees/{employee_id}/exit",
                json={
                    "last_working_day": (today - timedelta(days=2)).isoformat(),
                    "reason": "Test: resigned",
                },
            )
            assert left.status_code == 201, left.text
    await scan(api, Verdict.CLEAN)
    [version] = await api.records(DocumentVersion, {"id": uuid.UUID(document["versions"][0]["id"])})
    return Person(
        employee_id=employee_id,
        code=employee.employee_code,
        account=account,
        storage_key=version.storage_key,
        document_id=document["id"],
        version_id=document["versions"][0]["id"],
        department_id=units.department_id,
    )


async def stored_files(api: Api, key: str) -> int:
    return await api.worker_database.collection("files.files").count_documents(
        {"filename": key, "metadata.organization_id": api.organization_id}
    )


async def remove_files(api: Api, store: Any = None) -> int:
    with tenant_scope(api.organization_id):
        return await erasure_job.remove_files(
            api.worker_database, store or api.files, api.clock, AuditWriter(api.clock)
        )


class FailingStore:
    async def delete(self, key: str) -> None:
        raise FileStoreError("file storage is unavailable: test")


def categories(view: dict[str, Any]) -> dict[str, tuple[str, int]]:
    return {item["key"]: (item["outcome"], item["count"]) for item in view["result"]}


async def test_two_super_admins_erase_a_persons_data_and_the_report_says_what_remains(  # noqa: PLR0915 (one workflow end to end)
    files_api: Api,
) -> None:
    api = files_api
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    person = await a_person_with_data(api, hr)
    requester = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    approver = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    assert await stored_files(api, person.storage_key) == 1

    async with api.client() as client:
        await api.sign_in(client, requester)
        body = {"employee_code": person.code, "reason": REASON}
        assert (await client.post(ERASURES, json=body)).json()["type"] == "/problems/step-up-required"
        await api.step_up(client, requester)
        unknown = await client.post(ERASURES, json={**body, "employee_code": "NOPE-0000"})
        assert unknown.status_code == 422
        created = await client.post(ERASURES, json=body)
        assert created.status_code == 201, created.text
        request = created.json()
        assert request["status"] == "pending"
        assert request["employee"]["employee_code"] == person.code
        assert request["employee"]["name"].startswith("Test")
        assert request["result"] is None
        again = await client.post(ERASURES, json=body)
        assert (again.status_code, again.json()["code"]) == (409, "erasure.already_requested")
        # Never the requester's own decision (separation of duties).
        own = await client.post(f"{ERASURES}/{request['id']}/approve", json={"employee_code": person.code})
        assert (own.status_code, own.json()["code"]) == (403, "sod.own_request")

    async with api.client() as client:
        await api.sign_in(client, approver)
        path = f"{ERASURES}/{request['id']}/approve"
        assert (await client.post(path, json={"employee_code": person.code})).status_code == 403  # step-up
        await api.step_up(client, approver)
        wrong = await client.post(path, json={"employee_code": "TEST-WRONG"})
        assert (wrong.status_code, wrong.json()["errors"][0]["code"]) == (422, "erasure.code_mismatch")
        assert [e.legal_first_name for e in await api.records(Employee, {"id": person.employee_id})] == [
            "Test"
        ]
        approved = await client.post(path, json={"employee_code": person.code.lower(), "note": "Checked"})
        assert approved.status_code == 200, approved.text
        result = approved.json()
        twice = await client.post(path, json={"employee_code": person.code})
        assert (twice.status_code, twice.json()["code"]) == (409, "erasure.already_decided")
        # The records are erased; the file is waiting for the worker, so the request is not complete.
        assert result["status"] == "files_pending"
        assert (result["files_total"], result["files_remaining"]) == (1, 1)
        assert result["completed_at"] is None
        report = categories(result)
        assert report["people.personal_details"] == ("deleted", 1)
        assert report["people.emergency_contacts"] == ("deleted", 1)
        assert report["people.employee_record"] == ("anonymized", 1)
        assert report["people.job_notes"] == ("anonymized", 1)
        assert report["identity.account"] == ("anonymized", 1)
        assert report["identity.sign_in_credentials"][0] == "deleted"
        assert report["identity.sessions"][0] == "deleted"
        assert report["leave.request_reasons"] == ("anonymized", 1)
        assert report["attendance.correction_texts"] == ("anonymized", 1)
        assert report["attendance.events"] == ("retained", 2)
        assert report["documents.files"] == ("pending", 1)
        assert report["org.department_head"] == ("deleted", 1)
        assert report["audit.audit_log"][0] == "retained"
        assert report["audit.audit_log"][1] > 0
        assert report["people.status_history"] == ("retained", 1)
        assert result["employee"]["name"] == "Erased employee"
        # The erased file can never be downloaded, even before the worker removes it.
    async with api.client() as client:
        await signed_in(api, client, hr)
        download = await client.post(
            f"/api/v1/documents/{person.document_id}/versions/{person.version_id}/download"
        )
        assert download.status_code in (404, 409)

    # What was deleted is gone; what is history stays under the bare ID, anonymized.
    assert await api.records(EmployeePersonal, {"employee_id": person.employee_id}) == []
    assert await api.records(EmergencyContact, {"employee_id": person.employee_id}) == []
    [employee] = await api.records(Employee, {"id": person.employee_id})
    assert (employee.legal_first_name, employee.legal_last_name, employee.preferred_name) == (
        "Erased",
        "employee",
        None,
    )
    assert (employee.work_email, employee.work_phone) == (None, None)
    assert employee.erased_at is not None
    assert employee.employee_code.startswith("ERASED-")
    assert [job.notes for job in await api.records(EmployeeJob, {"employee_id": person.employee_id})] == [
        ERASED
    ]
    [user] = await api.records(User, {"id": person.account.user_id})
    assert user.email.endswith("@erased.invalid")
    assert user.status == "disabled"
    assert user.erased_at is not None
    for collection in ("credentials", "mfa_factors", "recovery_codes", "one_time_tokens", "trusted_devices"):
        assert (
            await api.count(
                collection,
                {"_id": person.account.user_id}
                if collection == "credentials"
                else {"user_id": person.account.user_id},
            )
            == 0
        ), collection
    assert await api.count("sessions", {"user_id": person.account.user_id}) == 0
    assert await api.count("notifications", {"recipient_user_id": person.account.user_id}) == 0
    assert [r.reason for r in await api.records(LeaveRequest, {"employee_id": person.employee_id})] == [
        ERASED
    ]
    [correction] = await api.records(AttendanceCorrection, {"employee_id": person.employee_id})
    assert correction.reason == ERASED
    assert await api.count("attendance_events", {"employee_id": person.employee_id}) == 2
    [version] = await api.records(DocumentVersion, {"id": uuid.UUID(person.version_id)})
    assert (version.original_filename, version.erased_at is not None) == (ERASED, True)
    assert await api.count("departments", {"_id": person.department_id, "head_employee_id": None}) == 1
    # The account can never sign in or be enabled again.
    async with api.client() as client:
        login = await client.post(
            "/api/v1/auth/login", json={"email": person.account.email, "password": "x" * 16}
        )
        assert login.status_code == 401
    system_admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    async with api.client() as client:
        await signed_in(api, client, system_admin)
        enable = await client.post(f"/api/v1/users/{person.account.user_id}/enable")
        assert (enable.status_code, enable.json()["code"]) == (409, "user.erased")
    # Nothing more is recorded about the person.
    async with api.client() as client:
        await signed_in(api, client, hr)
        personal = await client.put(
            f"/api/v1/employees/{person.employee_id}/emergency-contacts", json={"contacts": []}
        )
        assert personal.status_code in (409, 404, 405), personal.text

    # A storage failure keeps the request incomplete and says so; a later run finishes it.
    assert await remove_files(api, FailingStore()) == 0
    [waiting] = await api.records(ErasureRequest, {"id": uuid.UUID(request["id"])})
    assert (waiting.status, waiting.last_file_error, len(waiting.pending_storage_keys)) == (
        "files_pending",
        "FileStoreError",
        1,
    )
    assert await stored_files(api, person.storage_key) == 1
    assert await remove_files(api) == 1
    assert await stored_files(api, person.storage_key) == 0
    assert await remove_files(api) == 0  # nothing left to do: safe to run again
    async with api.client() as client:
        await api.sign_in(client, requester)
        done = (await client.get(f"{ERASURES}/{request['id']}")).json()
    assert (done["status"], done["files_remaining"], done["files_removed"]) == ("completed", 0, 1)
    assert categories(done)["documents.files"] == ("deleted", 1)
    assert done["completed_at"] is not None

    # The audit trail records who, when and what happened, never the person's details.
    rows = await api.records(
        AuditLogRecord, {"target_id": request["id"]}, sort=[("recorded_at", 1), ("id", 1)]
    )
    assert [row.action for row in rows] == [
        "people.erasure.requested",
        "people.employee.erased",
        "people.erasure.completed",
    ]
    assert rows[1].actor_user_id == approver.user_id
    assert rows[1].subject_employee_id == person.employee_id
    text = json.dumps([row.changes for row in rows], default=str) + "".join(str(row.reason) for row in rows)
    for personal_value in (
        "test.personal@dev.example",
        person.account.email,
        person.code,
        "Test Contact",
        "family matter",
    ):
        assert personal_value not in text


async def test_erasure_waits_for_offboarding_and_for_administrator_roles_to_go(files_api: Api) -> None:
    api = files_api
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    working = await a_person_with_data(api, hr, exited=False)
    admin_person = await a_person_with_data(api, hr)
    async with api.database.unit_of_work() as session:
        session.add(
            UserRole(
                user_id=admin_person.account.user_id,
                role_id=role_id("system_admin"),
                role_key="system_admin",
                valid_from=api.clock.now() - timedelta(minutes=1),
                grant_reason="Test assignment",
            )
        )
    requester = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    async with api.client() as client:
        await signed_in(api, client, requester)
        for person, code in ((working, "erasure.not_exited"), (admin_person, "erasure.admin_account")):
            refused = await client.post(ERASURES, json={"employee_code": person.code, "reason": REASON})
            assert (refused.status_code, refused.json()["code"]) == (409, code)
    assert await api.count("employee_erasures", {}) == 0


async def test_only_super_admins_reach_erasure(files_api: Api) -> None:
    api = files_api
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    person = await a_person_with_data(api, hr)
    for roles in (
        (),
        (RoleKey.HR.value,),
        (RoleKey.HR_ADMIN.value,),
        (RoleKey.SYSTEM_ADMIN.value,),
        (RoleKey.AUDITOR.value,),
    ):
        actor = await api.create_account(roles=roles)
        async with api.client() as client:
            await signed_in(api, client, actor)
            assert (await client.get(ERASURES)).status_code == 403, roles
            sent = await client.post(ERASURES, json={"employee_code": person.code, "reason": REASON})
            assert sent.status_code == 403, roles
    assert await api.count("employee_erasures", {}) == 0


async def test_a_failure_part_way_changes_nothing_and_the_request_can_be_approved_again(
    files_api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = files_api
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    person = await a_person_with_data(api, hr)
    requester = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    approver = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    async with api.client() as client:
        await signed_in(api, client, requester)
        request = (await client.post(ERASURES, json={"employee_code": person.code, "reason": REASON})).json()

    from app.modules.documents import public as documents  # noqa: PLC0415

    async def broken(*_: object) -> None:
        raise RuntimeError("test: storage marking failed")

    monkeypatch.setattr(documents, "erase_employee", broken)
    async with api.client() as client:
        await signed_in(api, client, approver)
        path = f"{ERASURES}/{request['id']}/approve"
        try:
            failed = await client.post(path, json={"employee_code": person.code})
            assert failed.status_code == 500
        except RuntimeError:
            pass
    # Identity, people and the other steps ran before the failure: none of it was kept.
    [user] = await api.records(User, {"id": person.account.user_id})
    assert (user.erased_at, user.email) == (None, person.account.email)
    assert len(await api.records(EmployeePersonal, {"employee_id": person.employee_id})) == 1
    assert await api.count("mfa_factors", {"user_id": person.account.user_id}) == 1
    [waiting] = await api.records(ErasureRequest, {"id": uuid.UUID(request["id"])})
    assert (waiting.status, waiting.result) == ("pending", None)
    monkeypatch.undo()
    async with api.client() as client:
        await signed_in(api, client, approver)
        retried = await client.post(path, json={"employee_code": person.code})
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "files_pending"


async def test_reject_and_withdraw(files_api: Api) -> None:
    api = files_api
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    person = await a_person_with_data(api, hr)
    requester = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    other = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    async with api.client() as a, api.client() as b:
        await signed_in(api, a, requester)
        await signed_in(api, b, other)
        first = (await a.post(ERASURES, json={"employee_code": person.code, "reason": REASON})).json()
        assert (await b.post(f"{ERASURES}/{first['id']}/cancel")).status_code == 403  # only the requester
        assert (await b.post(f"{ERASURES}/{first['id']}/reject", json={})).status_code == 422  # a note
        rejected = await b.post(f"{ERASURES}/{first['id']}/reject", json={"note": "Retention applies"})
        assert rejected.json()["status"] == "rejected"
        second = (await a.post(ERASURES, json={"employee_code": person.code, "reason": REASON})).json()
        withdrawn = await a.post(f"{ERASURES}/{second['id']}/cancel")
        assert withdrawn.json()["status"] == "cancelled"
    [employee] = await api.records(Employee, {"id": person.employee_id})
    assert employee.erased_at is None


async def test_erasable_fields_accept_only_the_erasure_marker(files_api: Api) -> None:
    api = files_api
    hr = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    person = await a_person_with_data(api, hr)
    with pytest.raises(GuardedUpdateError):  # noqa: PT012 (the flush raises)
        async with api.database.unit_of_work() as session:
            [correction] = await session.find(AttendanceCorrection, {"employee_id": person.employee_id})
            correction.reason = "Rewritten history"
            await session.flush()
    async with api.database.unit_of_work() as session:
        [correction] = await session.find(AttendanceCorrection, {"employee_id": person.employee_id})
        correction.reason = ERASED


async def test_another_organizations_super_admins_cannot_erase(migrated_mongo: MongoServer) -> None:
    async with (
        running_api(migrated_mongo, document_uploads_enabled=True) as a,
        running_api(migrated_mongo, document_uploads_enabled=True) as b,
    ):
        hr = await a.create_account(roles=(RoleKey.HR_ADMIN.value,))
        person = await a_person_with_data(a, hr)
        a_requester = await a.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
        async with a.client() as client:
            await signed_in(a, client, a_requester)
            request = (
                await client.post(ERASURES, json={"employee_code": person.code, "reason": REASON})
            ).json()
        b_admin = await b.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
        b.clock.current = max(a.clock.current, b.clock.current)
        async with b.client() as client:
            await signed_in(b, client, b_admin)
            # A's code is unknown in B, and A's request does not exist there.
            unknown = await client.post(ERASURES, json={"employee_code": person.code, "reason": REASON})
            assert unknown.status_code == 422
            assert (await client.get(f"{ERASURES}/{request['id']}")).status_code == 404
            approve = await client.post(
                f"{ERASURES}/{request['id']}/approve", json={"employee_code": person.code}
            )
            assert approve.status_code == 404
            assert [item["id"] for item in (await client.get(ERASURES)).json()["items"]] == []
        [employee] = await a.records(Employee, {"id": person.employee_id})
        assert employee.erased_at is None
