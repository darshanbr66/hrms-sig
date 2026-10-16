"""Documents (product-requirements DOC-1 to DOC-7, authorization-model §3.6).

Who may read a document depends on its category's sensitivity (DOC-2):

- the employee themselves (`document.read.self`) in categories marked employee-visible;
- the manager (`document.read.team`) in `internal` categories only;
- HR (`document.read.all`) in `internal` and `personal` categories;
- `document.sensitive.read.all` (step-up) in `sensitive` categories. Without a recent step-up a
  listing leaves sensitive documents out and says how many it left out.
- Company documents (no owner) are read with `document.company.read` and published with
  `document.company.manage`, in `internal` categories only.

Uploading: an employee adds their own documents in categories that allow it
(`document.upload.self`); HR adds and replaces documents for anyone in their scope
(`document.manage.all`). Each upload is a new version (DOC-6), limited to PDF, PNG and JPEG
decided by the file's content (DOC-5), at most 10 MB, stored privately under a random key.
A version can be downloaded only after the virus scan marked it clean; the API then issues a
download link valid for 60 seconds (DOC-3, rule 9), and every download is audited with who,
what, when and from where (DOC-4). Reads of someone else's documents are audited too.
"""

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.documents.models import (
    Document,
    DocumentCategory,
    DocumentVersion,
    ScanStatus,
    Sensitivity,
    Status,
)
from app.modules.identity import public as identity
from app.modules.notify.public import NotificationType, notify
from app.modules.people import public as people
from app.platform.audit.records import (
    AuditEvent,
    FieldChange,
    RedactedChange,
    RequestContext,
    ValueChange,
    change_of,
)
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer, StepUpRequired
from app.platform.clock import Clock
from app.platform.concurrency import check_version
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.files import DOWNLOAD_SECONDS, FileStore, FileStoreError
from app.platform.ratelimit import RateLimiter, RateLimitRule, RateLimitScope

# docs/api-architecture.md §9: fails closed (503) when the limit cannot be checked.
DOWNLOADS_PER_USER: Final = RateLimitRule(
    RateLimitScope.DOWNLOAD, "document_downloads", 60, timedelta(hours=1)
)
MAX_BYTES: Final = 10 * 1024 * 1024
# The allow-list, by the file's first bytes (DOC-5).
SIGNATURES: Final[tuple[tuple[bytes, str, str], ...]] = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
)
_UNSAFE_NAME: Final = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]+")


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def storage_missing() -> ProblemError:
    return ProblemError(
        ProblemType.TEMPORARILY_UNAVAILABLE,
        detail="File storage is not set up, so documents cannot be stored or downloaded. "
        "Ask your system administrator to configure it.",
        extensions={"code": "files.not_configured"},
    )


def sniff(data: bytes) -> tuple[str, str]:
    """The content type and extension the file's content shows, or a validation error."""
    for signature, content_type, extension in SIGNATURES:
        if data.startswith(signature):
            return content_type, extension
    raise field_error("file", "document.type_not_allowed", "Upload a PDF, PNG or JPEG file.")


def safe_filename(name: str | None, extension: str) -> str:
    """The uploaded file's name without any path, control or reserved characters."""
    base = unicodedata.normalize("NFC", (name or "").replace("\\", "/").rsplit("/", 1)[-1])
    base = _UNSAFE_NAME.sub("_", base).strip(" .")[:200]
    if not base:
        base = "document"
    if not base.lower().endswith(extension) and not (extension == ".jpg" and base.lower().endswith(".jpeg")):
        base += extension
    return base


@dataclass(frozen=True, slots=True)
class Upload:
    data: bytes
    filename: str | None


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    document: Document
    category: DocumentCategory
    owner_name: str | None
    versions: list[DocumentVersion]
    can_manage: bool
    can_add_version: bool


@dataclass(frozen=True, slots=True)
class Listing:
    records: list[DocumentRecord]
    # Sensitive documents left out because the reader has not stepped up recently.
    sensitive_hidden: int


def _changes(values: Mapping[str, Any], before: Any = None) -> dict[str, FieldChange]:
    return {
        name: change_of(name, getattr(before, name) if before is not None else None, value)
        for name, value in values.items()
        if before is None or getattr(before, name) != value
    }


class DocumentService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        audit: AuditWriter,
        authorizer: Authorizer,
        rate_limiter: RateLimiter,
        store: FileStore | None,
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer
        self._limiter = rate_limiter
        self._store = store

    @property
    def configured(self) -> bool:
        return self._store is not None

    def _require_store(self) -> FileStore:
        if self._store is None:
            raise storage_missing()
        return self._store

    # --- categories ----------------------------------------------------------------------------

    async def categories(self, *, include_archived: bool) -> list[DocumentCategory]:
        query = select(DocumentCategory).order_by(func.lower(DocumentCategory.name), DocumentCategory.id)
        if not include_archived:
            query = query.where(DocumentCategory.status == Status.ACTIVE.value)
        async with self._db.unit_of_work() as session:
            return list(await session.scalars(query))

    async def create_category(
        self, actor: Actor, context: RequestContext, values: Mapping[str, Any]
    ) -> DocumentCategory:
        self._check_category(values)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "document.category.manage")
            category = DocumentCategory(**values)
            session.add(category)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError as exc:
                raise conflict(
                    "document.category_code_in_use", "Another category already uses this code."
                ) from exc
            await self._audit.record(
                session,
                AuditEvent(
                    action="document.category.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="document_category",
                    target_id=category.id,
                    changes=_changes(values),
                ),
            )
            await session.refresh(category)
            return category

    @staticmethod
    def _check_category(values: Mapping[str, Any]) -> None:
        if values.get("employee_can_upload") and values.get("employee_visible") is False:
            raise field_error(
                "employee_can_upload",
                "document.upload_needs_visible",
                "Employees can upload only to categories they can see.",
            )

    async def update_category(
        self,
        actor: Actor,
        context: RequestContext,
        category_id: uuid.UUID,
        *,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> DocumentCategory:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "document.category.manage")
            category = await session.get(
                DocumentCategory, category_id, with_for_update=True, populate_existing=True
            )
            if category is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            check_version(category.version, expected_version)
            merged = {
                "employee_visible": values.get("employee_visible", category.employee_visible),
                "employee_can_upload": values.get("employee_can_upload", category.employee_can_upload),
            }
            self._check_category(merged)
            if "sensitivity" in values and values["sensitivity"] != category.sensitivity:
                in_use = (
                    await session.execute(select(func.count()).where(Document.category_id == category.id))
                ).scalar_one()
                if in_use:
                    raise conflict(
                        "document.category_in_use",
                        "Documents already use this category, so its sensitivity cannot change. "
                        "Create a new category instead.",
                    )
            changes = _changes(values, category)
            if changes:
                for name, value in values.items():
                    setattr(category, name, value)
                category.version += 1
                category.updated_at = self._clock.now()
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="document.category.updated",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=granted.key,
                        target_type="document_category",
                        target_id=category.id,
                        changes=changes,
                    ),
                )
            await session.flush()
            await session.refresh(category)
            return category

    # --- who may do what -----------------------------------------------------------------------

    async def _read_permission(
        self, session: AsyncSession, actor: Actor, owner: uuid.UUID | None, category: DocumentCategory
    ) -> str | None:
        """The narrowest permission that lets the actor read documents of this owner and
        category, or None. Raises StepUpRequired for a sensitive category without step-up."""
        if owner is None:
            if category.sensitivity != Sensitivity.INTERNAL:
                return None
            for key in ("document.company.read", "document.company.manage"):
                if actor.holds(key):
                    return key
            return None
        keys: list[str] = []
        if category.employee_visible:
            keys.append("document.read.self")
        if category.sensitivity == Sensitivity.INTERNAL:
            keys += ["document.read.team", "document.read.all"]
        elif category.sensitivity == Sensitivity.PERSONAL:
            keys.append("document.read.all")
        else:
            keys.append("document.sensitive.read.all")
        for key in keys:
            if not actor.holds(key):
                continue
            try:
                await self._authz.require(session, actor, key, subject_employee_id=owner)
            except AccessDenied:
                continue
            return key
        return None

    async def _can_manage(self, session: AsyncSession, actor: Actor, owner: uuid.UUID | None) -> str | None:
        key = "document.company.manage" if owner is None else "document.manage.all"
        if not actor.holds(key):
            return None
        try:
            await self._authz.require(session, actor, key, subject_employee_id=owner)
        except AccessDenied:
            return None
        return key

    def _self_upload(self, actor: Actor, owner: uuid.UUID | None, category: DocumentCategory) -> bool:
        return (
            owner is not None
            and owner == actor.employee_id
            and category.employee_can_upload
            and actor.holds("document.upload.self")
        )

    # --- reading -------------------------------------------------------------------------------

    async def _records(
        self, session: AsyncSession, actor: Actor, documents: Sequence[Document]
    ) -> tuple[list[DocumentRecord], int, set[str]]:
        categories = (
            {
                c.id: c
                for c in await session.scalars(
                    select(DocumentCategory).where(
                        DocumentCategory.id.in_({d.category_id for d in documents})
                    )
                )
            }
            if documents
            else {}
        )
        versions: dict[uuid.UUID, list[DocumentVersion]] = {d.id: [] for d in documents}
        if documents:
            for version in await session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id.in_(list(versions)))
                .order_by(DocumentVersion.document_id, DocumentVersion.version_no.desc())
            ):
                versions[version.document_id].append(version)
        names = await people.employee_names(
            session, {d.owner_employee_id for d in documents if d.owner_employee_id}
        )
        records, hidden, used = [], 0, set()
        for document in documents:
            category = categories[document.category_id]
            try:
                key = await self._read_permission(session, actor, document.owner_employee_id, category)
            except StepUpRequired:
                hidden += 1
                continue
            if key is None:
                continue
            used.add(key)
            manage = await self._can_manage(session, actor, document.owner_employee_id) is not None
            records.append(
                DocumentRecord(
                    document=document,
                    category=category,
                    owner_name=names.get(document.owner_employee_id) if document.owner_employee_id else None,
                    versions=versions[document.id],
                    can_manage=manage,
                    can_add_version=manage or self._self_upload(actor, document.owner_employee_id, category),
                )
            )
        return records, hidden, used

    async def employee_documents(
        self, actor: Actor, context: RequestContext, employee_id: uuid.UUID | None, *, include_archived: bool
    ) -> Listing:
        """An employee's documents the actor may read (their own when no ID is given)."""
        owner = employee_id or _own_employee(actor)
        async with self._db.unit_of_work() as session:
            if not await people.employee_exists(session, owner):
                raise ProblemError(ProblemType.NOT_FOUND)
            query = select(Document).where(Document.owner_employee_id == owner)
            if not include_archived:
                query = query.where(Document.status == Status.ACTIVE.value)
            documents = list(
                await session.scalars(query.order_by(Document.created_at.desc(), Document.id.desc()))
            )
            if owner != actor.employee_id and not await self._reaches(session, actor, owner):
                # Someone outside every document scope learns nothing about the record.
                raise ProblemError(ProblemType.NOT_FOUND)
            records, hidden, used = await self._records(session, actor, documents)
            if owner != actor.employee_id and records:
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="document.listed",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=sorted(used)[0],
                        target_type="employee",
                        target_id=owner,
                        subject_employee_id=owner,
                        changes={"documents": ValueChange(None, len(records))},
                    ),
                )
            return Listing(records, hidden)

    async def _reaches(self, session: AsyncSession, actor: Actor, owner: uuid.UUID) -> bool:
        """Whether some document permission the actor holds covers this employee."""
        for key in (
            "document.read.team",
            "document.read.all",
            "document.sensitive.read.all",
            "document.manage.all",
        ):
            if not actor.holds(key):
                continue
            try:
                await self._authz.require(session, actor, key, subject_employee_id=owner)
            except AccessDenied:
                continue
            except StepUpRequired:
                return True
            return True
        return False

    async def company_documents(self, actor: Actor, *, include_archived: bool) -> Listing:
        async with self._db.unit_of_work() as session:
            query = select(Document).where(Document.owner_employee_id.is_(None))
            if not include_archived:
                query = query.where(Document.status == Status.ACTIVE.value)
            documents = list(
                await session.scalars(query.order_by(Document.created_at.desc(), Document.id.desc()))
            )
            records, hidden, _ = await self._records(session, actor, documents)
            return Listing(records, hidden)

    async def detail(self, actor: Actor, document_id: uuid.UUID) -> DocumentRecord:
        async with self._db.unit_of_work() as session:
            return await self._readable(session, actor, document_id)

    async def _readable(self, session: AsyncSession, actor: Actor, document_id: uuid.UUID) -> DocumentRecord:
        document = await session.get(Document, document_id, populate_existing=True)
        if document is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        category = await session.get(DocumentCategory, document.category_id)
        if category is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        if await self._read_permission(session, actor, document.owner_employee_id, category) is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        records, _, _ = await self._records(session, actor, [document])
        return records[0]

    async def download(
        self, actor: Actor, context: RequestContext, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> tuple[str, int]:
        """A 60-second link to one clean version, after authorizing and auditing the download."""
        await self._limiter.enforce(DOWNLOADS_PER_USER, str(actor.user_id))
        async with self._db.unit_of_work() as session:
            document = await session.get(Document, document_id)
            category = await session.get(DocumentCategory, document.category_id) if document else None
            if document is None or category is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            key = await self._read_permission(session, actor, document.owner_employee_id, category)
            if key is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            version = await session.get(DocumentVersion, version_id)
            if version is None or version.document_id != document.id:
                raise ProblemError(ProblemType.NOT_FOUND)
            if version.scan_status == ScanStatus.PENDING:
                raise conflict(
                    "document.scan_pending",
                    "This file is still being checked for viruses. Try again in a minute.",
                )
            if version.scan_status != ScanStatus.CLEAN:
                raise conflict(
                    "document.unavailable",
                    "This file did not pass the virus check, so it cannot be downloaded. Upload it again.",
                )
            store = self._require_store()
            await self._audit.record(
                session,
                AuditEvent(
                    action="document.downloaded",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=key,
                    target_type="document_version",
                    target_id=version.id,
                    subject_employee_id=document.owner_employee_id,
                    changes={
                        "document_id": ValueChange(None, document.id),
                        "version_no": ValueChange(None, version.version_no),
                    },
                ),
            )
            try:
                url = await store.download_url(
                    version.storage_key, filename=version.original_filename, content_type=version.content_type
                )
            except FileStoreError as exc:
                raise ProblemError(ProblemType.TEMPORARILY_UNAVAILABLE) from exc
        return url, DOWNLOAD_SECONDS

    # --- uploading -----------------------------------------------------------------------------

    async def upload(
        self,
        actor: Actor,
        context: RequestContext,
        *,
        owner: uuid.UUID | None,
        own: bool,
        category_id: uuid.UUID,
        title: str,
        expires_on: date | None,
        upload: Upload,
    ) -> DocumentRecord:
        """A new document with its first version. `own` uploads to the actor's own record."""
        owner = _own_employee(actor) if own else owner
        async with self._db.unit_of_work() as session:
            category = await session.get(DocumentCategory, category_id)
            if category is None or category.status != Status.ACTIVE:
                raise field_error("category_id", "document.unknown_category", "Choose an active category.")
            if owner is not None and not await people.employee_exists(session, owner):
                raise ProblemError(ProblemType.NOT_FOUND)
            permission = await self._upload_permission(session, actor, owner, category, own=own)
            if owner is None and category.sensitivity != Sensitivity.INTERNAL:
                raise field_error(
                    "category_id",
                    "document.company_category",
                    "Company documents go in an internal category.",
                )
            if category.requires_expiry and expires_on is None:
                raise field_error(
                    "expires_on", "document.expiry_required", "This category needs an expiry date."
                )
            store = self._require_store()
            document = Document(
                owner_employee_id=owner,
                category_id=category.id,
                title=title,
                expires_on=expires_on,
                created_by=actor.user_id,
            )
            session.add(document)
            await session.flush()
            version = await self._store_version(session, store, actor, document, number=1, upload=upload)
            await self._audit.record(
                session,
                AuditEvent(
                    action="document.uploaded",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=permission,
                    target_type="document",
                    target_id=document.id,
                    subject_employee_id=owner,
                    changes={
                        "category_id": ValueChange(None, category.id),
                        "title": RedactedChange(),
                        "version_no": ValueChange(None, 1),
                        "content_type": ValueChange(None, version.content_type),
                        "size_bytes": ValueChange(None, version.size_bytes),
                    },
                ),
            )
            await self._tell_owner(session, actor, document, category)
            records, _, _ = await self._records(session, actor, [document])
            if records:
                return records[0]
            # Written but not readable by the uploader (a sensitive category without step-up,
            # or HR who may manage but not read it): answer with what they sent.
            return DocumentRecord(document, category, None, [version], True, True)

    async def add_version(
        self, actor: Actor, context: RequestContext, document_id: uuid.UUID, upload: Upload
    ) -> DocumentRecord:
        async with self._db.unit_of_work() as session:
            document = await session.get(Document, document_id, with_for_update=True, populate_existing=True)
            category = await session.get(DocumentCategory, document.category_id) if document else None
            if document is None or category is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            own = document.owner_employee_id is not None and document.owner_employee_id == actor.employee_id
            permission = await self._upload_permission(
                session, actor, document.owner_employee_id, category, own=own
            )
            if document.status != Status.ACTIVE:
                raise conflict("document.archived", "This document is archived. Upload it as a new document.")
            store = self._require_store()
            number = (
                await session.execute(
                    select(func.coalesce(func.max(DocumentVersion.version_no), 0)).where(
                        DocumentVersion.document_id == document.id
                    )
                )
            ).scalar_one() + 1
            version = await self._store_version(session, store, actor, document, number=number, upload=upload)
            document.version += 1
            document.updated_at = self._clock.now()
            await self._audit.record(
                session,
                AuditEvent(
                    action="document.version.uploaded",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=permission,
                    target_type="document",
                    target_id=document.id,
                    subject_employee_id=document.owner_employee_id,
                    changes={
                        "version_no": ValueChange(None, number),
                        "content_type": ValueChange(None, version.content_type),
                        "size_bytes": ValueChange(None, version.size_bytes),
                    },
                ),
            )
            await session.flush()
            records, _, _ = await self._records(session, actor, [document])
            return records[0] if records else DocumentRecord(document, category, None, [version], True, True)

    async def _upload_permission(
        self,
        session: AsyncSession,
        actor: Actor,
        owner: uuid.UUID | None,
        category: DocumentCategory,
        *,
        own: bool,
    ) -> str:
        if own and self._self_upload(actor, owner, category):
            return "document.upload.self"
        key = await self._can_manage(session, actor, owner)
        if key is None:
            if own and actor.holds("document.upload.self"):
                raise field_error(
                    "category_id",
                    "document.self_upload_not_allowed",
                    "You can't upload to this category. Choose another, or ask HR.",
                )
            raise AccessDenied(hide_existence=owner is not None and not own)
        return key

    async def _store_version(
        self,
        session: AsyncSession,
        store: FileStore,
        actor: Actor,
        document: Document,
        *,
        number: int,
        upload: Upload,
    ) -> DocumentVersion:
        if not upload.data:
            raise field_error("file", "document.empty", "The file is empty. Choose another file.")
        if len(upload.data) > MAX_BYTES:
            raise field_error("file", "document.too_large", "Upload a file of at most 10 MB.")
        content_type, extension = sniff(upload.data)
        key = f"documents/{uuid.uuid4()}"
        version = DocumentVersion(
            document_id=document.id,
            version_no=number,
            storage_key=key,
            original_filename=safe_filename(upload.filename, extension),
            content_type=content_type,
            size_bytes=len(upload.data),
            sha256=hashlib.sha256(upload.data).digest(),
            uploaded_by=actor.user_id,
        )
        session.add(version)
        await session.flush()
        try:
            # Stored before the transaction commits: if storing fails nothing is recorded; if
            # the commit fails afterwards the object is orphaned, never exposed (no row names it).
            await store.put(key, upload.data, content_type)
        except FileStoreError as exc:
            raise ProblemError(ProblemType.TEMPORARILY_UNAVAILABLE) from exc
        return version

    async def _tell_owner(
        self, session: AsyncSession, actor: Actor, document: Document, category: DocumentCategory
    ) -> None:
        if document.owner_employee_id is None or document.owner_employee_id == actor.employee_id:
            return
        if not category.employee_visible:
            return
        user_id = await identity.account_for_employee(session, document.owner_employee_id)
        if user_id is None:
            return
        await notify(
            session,
            user_id=user_id,
            kind=NotificationType.DOCUMENT_ADDED,
            title="A document was added for you",
            body=f"HR added a document in {category.name}.",
            link="/documents",
            idempotency_key=f"document:{document.id}:added:{user_id}",
            subject=("document", document.id),
        )

    # --- changing ------------------------------------------------------------------------------

    async def update(
        self,
        actor: Actor,
        context: RequestContext,
        document_id: uuid.UUID,
        *,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> DocumentRecord:
        async with self._db.unit_of_work() as session:
            document = await session.get(Document, document_id, with_for_update=True, populate_existing=True)
            category = await session.get(DocumentCategory, document.category_id) if document else None
            if document is None or category is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            permission = await self._can_manage(session, actor, document.owner_employee_id)
            if permission is None:
                if await self._read_permission(session, actor, document.owner_employee_id, category):
                    raise AccessDenied
                raise ProblemError(ProblemType.NOT_FOUND)
            check_version(document.version, expected_version)
            if category.requires_expiry and "expires_on" in values and values["expires_on"] is None:
                raise field_error(
                    "expires_on", "document.expiry_required", "This category needs an expiry date."
                )
            changes = {
                name: RedactedChange() if name == "title" else change
                for name, change in _changes(values, document).items()
            }
            if changes:
                for name, value in values.items():
                    setattr(document, name, value)
                document.version += 1
                document.updated_at = self._clock.now()
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="document.updated",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=permission,
                        target_type="document",
                        target_id=document.id,
                        subject_employee_id=document.owner_employee_id,
                        changes=changes,
                    ),
                )
            await session.flush()
            records, _, _ = await self._records(session, actor, [document])
            return records[0] if records else DocumentRecord(document, category, None, [], True, True)


def _own_employee(actor: Actor) -> uuid.UUID:
    if actor.employee_id is None:
        raise ProblemError(ProblemType.NOT_FOUND, detail="Your account is not linked to an employee record.")
    return actor.employee_id
