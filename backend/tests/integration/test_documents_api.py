"""Documents over HTTP with private storage on the S3 emulator (product-requirements DOC-1 to
DOC-6, authorization-model §3.6). Files are tiny, obviously fake test files."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import httpx

from app.modules.documents.scan_job import MAX_ATTEMPTS, scan_pending
from app.platform.audit.writer import AuditWriter
from app.platform.authz.roles import RoleKey
from app.platform.scanning import ScanError, ScanResult, Verdict
from tests.api_support import Account, Api
from tests.people_data import add_job, create_employee, create_org_units, fake_code

PDF = b"%PDF-1.4\n% test document\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeScanner:
    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict

    async def scan(self, data: bytes) -> ScanResult:
        return ScanResult(self.verdict, "Test-Signature" if self.verdict is Verdict.INFECTED else None)


class UnavailableScanner:
    """ClamAV cannot be reached for one file (its exact bytes); other waiting files scan clean,
    so earlier tests' uploads do not stand in the way."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def scan(self, data: bytes) -> ScanResult:
        if data == self.body:
            raise ScanError("the scanner is unavailable: ConnectionRefusedError")
        return ScanResult(Verdict.CLEAN)


@dataclass
class People:
    admin: Account
    hr: Account
    manager: Account
    employee: Account
    outsider: Account


async def people(api: Api) -> People:
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        manager = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(session, units, manager, manager_id=None, effective_from=today - timedelta(days=30))
        employee = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(session, units, employee, manager_id=manager, effective_from=today - timedelta(days=30))
        outsider = await create_employee(session, joined=today - timedelta(days=30))
        await add_job(session, units, outsider, manager_id=None, effective_from=today - timedelta(days=30))
    return People(
        admin=await api.create_account(roles=(RoleKey.HR_ADMIN.value,)),
        hr=await api.create_account(roles=(RoleKey.HR.value,)),
        manager=await api.create_account(employee_id=manager),
        employee=await api.create_account(employee_id=employee),
        outsider=await api.create_account(employee_id=outsider),
    )


async def category(client: httpx.AsyncClient, sensitivity: str, *, visible: bool, upload: bool) -> str:
    created = await client.post(
        "/api/v1/document-categories",
        json={
            "code": fake_code("DC"),
            "name": f"Test {sensitivity} documents",
            "sensitivity": sensitivity,
            "employee_visible": visible,
            "employee_can_upload": upload,
            "requires_expiry": False,
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def upload(
    client: httpx.AsyncClient, path: str, category_id: str, body: bytes = PDF, name: str = "test.pdf"
) -> httpx.Response:
    return await client.post(
        path,
        params={"category_id": category_id, "title": "Test document"},
        content=body,
        headers={"X-File-Name": name, "Content-Type": "application/octet-stream"},
    )


async def scan(api: Api, verdict: Verdict) -> None:
    assert api.files is not None
    await scan_pending(
        api.worker_database, api.files, FakeScanner(verdict), api.clock, AuditWriter(api.clock)
    )


async def test_documents_are_stored_privately_scanned_and_downloaded_through_short_links(
    files_api: Api,
) -> None:
    api = files_api
    p = await people(api)
    async with api.client() as client:
        await api.sign_in(client, p.admin)
        letters = await category(client, "internal", visible=True, upload=True)
        notes = await category(client, "personal", visible=False, upload=False)
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        assert (await upload(client, "/api/v1/me/documents", notes)).json()["errors"][0]["code"] in {
            "document.self_upload_not_allowed",
            "document.unknown_category",
        }
        executable = await upload(
            client, "/api/v1/me/documents", letters, body=b"MZ\x90\x00 not a pdf", name="x.pdf"
        )
        assert executable.json()["errors"][0]["code"] == "document.type_not_allowed"
        too_big = await upload(client, "/api/v1/me/documents", letters, body=PDF + b"0" * (10 * 1024 * 1024))
        assert too_big.json()["errors"][0]["code"] == "document.too_large"
        created = await upload(client, "/api/v1/me/documents", letters, name="../../My offer letter.pdf")
        assert created.status_code == 201, created.text
        document = created.json()
        [version] = document["versions"]
        assert version["scan_status"] == "pending"
        assert version["content_type"] == "application/pdf"
        assert version["original_filename"] == "My offer letter.pdf"
        waiting = await client.post(f"/api/v1/documents/{document['id']}/versions/{version['id']}/download")
        assert waiting.json()["code"] == "document.scan_pending"

    await scan(api, Verdict.CLEAN)
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        link = await client.post(f"/api/v1/documents/{document['id']}/versions/{version['id']}/download")
        assert link.status_code == 200, link.text
        assert link.json()["expires_in_seconds"] == 60
        # The signed link itself stops working after 60 seconds (rule 9).
        assert "X-Amz-Expires=60" in link.json()["url"]
        async with httpx.AsyncClient() as plain:
            fetched = await plain.get(link.json()["url"])
        assert fetched.status_code == 200
        assert fetched.content == PDF
        assert fetched.headers["content-disposition"].startswith("attachment;")
        newer = await client.post(
            f"/api/v1/documents/{document['id']}/versions",
            content=PDF + b"% second\n",
            headers={"X-File-Name": "offer.pdf"},
        )
        assert [v["version_no"] for v in newer.json()["versions"]] == [2, 1]
    [(downloads,)] = await api.fetch(
        "SELECT count(*) FROM audit.audit_log "
        "WHERE action = 'document.downloaded' AND subject_employee_id = :id",
        id=p.employee.employee_id,
    )
    assert downloads == 1
    # The storage key is random, never the uploaded name.
    [(key,)] = await api.fetch(
        "SELECT storage_key FROM docs.document_versions WHERE id = :id", id=version["id"]
    )
    assert key.startswith("documents/")
    assert "offer" not in key


async def test_who_reads_what_follows_the_category_sensitivity(files_api: Api) -> None:
    api = files_api
    p = await people(api)
    async with api.client() as client:
        await api.sign_in(client, p.admin)
        internal = await category(client, "internal", visible=True, upload=False)
        personal = await category(client, "personal", visible=False, upload=False)
        sensitive = await category(client, "sensitive", visible=True, upload=False)
        path = f"/api/v1/employees/{p.employee.employee_id}/documents"
        for category_id in (internal, personal):
            assert (await upload(client, path, category_id)).status_code == 201
        assert (await upload(client, path, sensitive, body=PNG, name="id.png")).status_code == 201
        hidden = (await client.get(path)).json()
        assert len(hidden["items"]) == 2
        assert hidden["sensitive_hidden"] == 1
        await api.step_up(client, p.admin)
        assert len((await client.get(path)).json()["items"]) == 3
    async with api.client() as client:
        await api.sign_in(client, p.hr)
        hr_view = (await client.get(path)).json()
        assert {d["category"]["sensitivity"] for d in hr_view["items"]} == {"internal", "personal"}
        assert hr_view["sensitive_hidden"] == 0
    async with api.client() as client:
        await api.sign_in(client, p.manager)
        assert {d["category"]["sensitivity"] for d in (await client.get(path)).json()["items"]} == {
            "internal"
        }
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        mine = (await client.get("/api/v1/me/documents")).json()
        assert {d["category"]["sensitivity"] for d in mine["items"]} == {"internal", "sensitive"}
        notes = (await client.get("/api/v1/me/notifications")).json()["items"]
        assert sum(n["type"] == "document.added" for n in notes) == 2
    async with api.client() as client:
        await api.sign_in(client, p.outsider)
        assert (await client.get(path)).status_code == 404


async def test_an_infected_file_is_never_downloadable(files_api: Api) -> None:
    api = files_api
    p = await people(api)
    async with api.client() as client:
        await api.sign_in(client, p.admin)
        internal = await category(client, "internal", visible=True, upload=True)
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        document = (await upload(client, "/api/v1/me/documents", internal)).json()
    await scan(api, Verdict.INFECTED)
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        version_id = document["versions"][0]["id"]
        refused = await client.post(f"/api/v1/documents/{document['id']}/versions/{version_id}/download")
        assert refused.json()["code"] == "document.unavailable"
        detail = (await client.get(f"/api/v1/documents/{document['id']}")).json()
        assert detail["versions"][0]["scan_status"] == "infected"
    [(flagged,)] = await api.fetch(
        "SELECT count(*) FROM audit.audit_log WHERE action = 'document.scan.infected' AND target_id = :id",
        id=version_id,
    )
    assert flagged == 1
    async with api.client() as client:
        await api.sign_in(client, p.hr)
        detail = (await client.get(f"/api/v1/documents/{document['id']}")).json()
        archived = await client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"status": "archived"},
            headers={"If-Match": f'"{detail["version"]}"'},
        )
        assert archived.json()["status"] == "archived"


async def test_a_file_the_scanner_cannot_check_is_never_downloadable(files_api: Api) -> None:
    """Fail closed: while the scanner is unavailable the version waits, and after the last
    attempt it is marked failed, which is never downloadable either."""
    api = files_api
    p = await people(api)
    body = PDF + b"% the scanner never answers for this one\n"
    async with api.client() as client:
        await api.sign_in(client, p.admin)
        internal = await category(client, "internal", visible=True, upload=True)
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        document = (await upload(client, "/api/v1/me/documents", internal, body=body)).json()
    version_id = document["versions"][0]["id"]
    download = f"/api/v1/documents/{document['id']}/versions/{version_id}/download"
    assert api.files is not None
    scanner = UnavailableScanner(body)

    async def attempt() -> tuple[str, int]:
        assert api.files is not None
        await scan_pending(api.worker_database, api.files, scanner, api.clock, AuditWriter(api.clock))
        [(status, attempts)] = await api.fetch(
            "SELECT scan_status, scan_attempts FROM docs.document_versions WHERE id = :id", id=version_id
        )
        return status, attempts

    assert await attempt() == ("pending", 1)
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        assert (await client.post(download)).json()["code"] == "document.scan_pending"
    for _ in range(MAX_ATTEMPTS - 1):
        status, attempts = await attempt()
    assert (status, attempts) == ("failed", MAX_ATTEMPTS)
    async with api.client() as client:
        await api.sign_in(client, p.employee)
        refused = await client.post(download)
        assert refused.status_code == 409
        assert refused.json()["code"] == "document.unavailable"


async def test_without_storage_the_api_says_it_is_not_set_up(api: Api) -> None:
    admin = await api.create_account(roles=(RoleKey.HR_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, admin)
        internal = await category(client, "internal", visible=True, upload=True)
        # Checks that do not need storage still answer first.
        assert (
            await upload(client, f"/api/v1/employees/{uuid.uuid4()}/documents", internal)
        ).status_code == 404
        refused = await upload(client, "/api/v1/company-documents", internal)
        assert refused.status_code == 503
        assert refused.json()["code"] == "files.not_configured"
