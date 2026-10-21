"""HTTP routes for documents (docs/api-architecture.md §8, Documents).

Uploads send the file itself as the request body (`Content-Type: application/octet-stream`),
with the details as query parameters and the file's name in `X-File-Name` (percent-encoded).
The stored type comes from the file's content, never from these headers.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Final, Literal
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.modules.documents.models import DocumentCategory
from app.modules.documents.service import (
    MAX_BYTES,
    DocumentRecord,
    DocumentService,
    Listing,
    Upload,
    field_error,
)
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.concurrency import IfMatch, etag, expected_version

router = APIRouter(prefix="/api/v1", tags=["documents"])

type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
type Code = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^\S+$")
]
type Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
type SensitivityValue = Literal["internal", "personal", "sensitive"]
Title = Annotated[str, Query(min_length=1, max_length=200)]
FileName = Annotated[str | None, Header(alias="X-File-Name", max_length=1000)]
READ_CHUNK: Final = 64 * 1024


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryCreate(_Request):
    code: Code
    name: Name
    description: Description | None = None
    sensitivity: SensitivityValue
    employee_visible: bool
    employee_can_upload: bool
    requires_expiry: bool


class CategoryUpdate(_Request):
    name: Name | None = None
    description: Description | None = None
    sensitivity: SensitivityValue | None = None
    employee_visible: bool | None = None
    employee_can_upload: bool | None = None
    requires_expiry: bool | None = None
    status: Literal["active", "archived"] | None = None


class CategoryView(_Response):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    sensitivity: SensitivityValue
    employee_visible: bool
    employee_can_upload: bool
    requires_expiry: bool
    status: Literal["active", "archived"]
    version: int


class CategoryList(_Response):
    items: list[CategoryView]


class VersionView(_Response):
    id: uuid.UUID
    version_no: int
    original_filename: str
    content_type: str
    size_bytes: int
    # pending: being checked for viruses; clean: downloadable; infected or failed: never.
    scan_status: Literal["pending", "clean", "infected", "failed"]
    uploaded_at: datetime


class DocumentView(_Response):
    id: uuid.UUID
    owner_employee_id: uuid.UUID | None
    owner_name: str | None
    category: CategoryView
    title: str
    expires_on: date | None
    status: Literal["active", "archived"]
    version: int
    created_at: datetime
    versions: list[VersionView]
    can_manage: bool
    can_add_version: bool


class DocumentList(_Response):
    items: list[DocumentView]
    # Sensitive documents left out until the reader verifies with their authenticator.
    sensitive_hidden: int
    # Whether file storage is available. Files live in the database (GridFS, ADR-030), so it
    # always is; kept so clients of the published contract need no change.
    storage_ready: bool
    # False when uploads are refused: turned off while virus scanning is not set up (DOC-5).
    uploads_ready: bool


class DocumentUpdate(_Request):
    title: Name | None = None
    expires_on: date | None = None
    status: Literal["active", "archived"] | None = None


class DocumentDownloadLink(_Response):
    url: str
    expires_in_seconds: int


def service(request: Request) -> DocumentService:
    value: DocumentService = request.app.state.document_service
    return value


Service = Annotated[DocumentService, Depends(service)]


def _sent(body: BaseModel) -> dict[str, Any]:
    return {name: getattr(body, name) for name in body.model_fields_set}


def _category(category: DocumentCategory) -> CategoryView:
    return CategoryView(
        id=category.id,
        code=category.code,
        name=category.name,
        description=category.description,
        sensitivity=category.sensitivity,
        employee_visible=category.employee_visible,
        employee_can_upload=category.employee_can_upload,
        requires_expiry=category.requires_expiry,
        status=category.status,
        version=category.version,
    )


def _document(record: DocumentRecord, response: Response | None = None) -> DocumentView:
    d = record.document
    if response is not None:
        response.headers["ETag"] = etag(d.version)
    return DocumentView(
        id=d.id,
        owner_employee_id=d.owner_employee_id,
        owner_name=record.owner_name,
        category=_category(record.category),
        title=d.title,
        expires_on=d.expires_on,
        status=d.status,
        version=d.version,
        created_at=d.created_at,
        versions=[
            VersionView(
                id=v.id,
                version_no=v.version_no,
                original_filename=v.original_filename,
                content_type=v.content_type,
                size_bytes=v.size_bytes,
                scan_status=v.scan_status,
                uploaded_at=v.uploaded_at,
            )
            for v in record.versions
        ],
        can_manage=record.can_manage,
        can_add_version=record.can_add_version,
    )


def _listing(listing: Listing, documents: DocumentService) -> DocumentList:
    return DocumentList(
        items=[_document(r) for r in listing.records],
        sensitive_hidden=listing.sensitive_hidden,
        storage_ready=True,
        uploads_ready=documents.uploads_ready,
    )


async def _read_upload(request: Request, file_name: str | None) -> Upload:
    """The request body, refused as soon as it passes the size limit."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise field_error("file", "document.too_large", "Upload a file of at most 10 MB.")
    return Upload(bytes(data), unquote(file_name) if file_name else None)


# --- categories -------------------------------------------------------------------------------


@router.get("/document-categories", response_model=CategoryList)
async def list_document_categories(
    documents: Service, _actor: Annotated[Actor, requires("document.read")], include_archived: bool = False
) -> CategoryList:
    return CategoryList(
        items=[_category(c) for c in await documents.categories(include_archived=include_archived)]
    )


@router.post("/document-categories", response_model=CategoryView, status_code=status.HTTP_201_CREATED)
async def create_document_category(
    body: CategoryCreate,
    request: Request,
    documents: Service,
    actor: Annotated[Actor, requires("document.category.manage")],
) -> CategoryView:
    return _category(
        await documents.create_category(actor, RequestContext.from_request(request), body.model_dump())
    )


@router.patch("/document-categories/{category_id}", response_model=CategoryView)
async def update_document_category(
    *,
    category_id: uuid.UUID,
    body: CategoryUpdate,
    request: Request,
    response: Response,
    documents: Service,
    actor: Annotated[Actor, requires("document.category.manage")],
    if_match: IfMatch = None,
) -> CategoryView:
    values = _sent(body)
    for name in (
        "name",
        "sensitivity",
        "employee_visible",
        "employee_can_upload",
        "requires_expiry",
        "status",
    ):
        if name in values and values[name] is None:
            raise field_error(name, "document.required", "Choose a value.")
    category = await documents.update_category(
        actor,
        RequestContext.from_request(request),
        category_id,
        expected_version=expected_version(if_match),
        values=values,
    )
    response.headers["ETag"] = etag(category.version)
    return _category(category)


# --- reading ----------------------------------------------------------------------------------


@router.get("/me/documents", response_model=DocumentList)
async def my_documents(
    request: Request, documents: Service, actor: Annotated[Actor, requires("document.read.self")]
) -> DocumentList:
    listing = await documents.employee_documents(
        actor, RequestContext.from_request(request), None, include_archived=False
    )
    return _listing(listing, documents)


@router.get("/employees/{employee_id}/documents", response_model=DocumentList)
async def employee_documents(
    employee_id: uuid.UUID,
    request: Request,
    documents: Service,
    actor: Annotated[Actor, requires("document.read")],
    include_archived: bool = False,
) -> DocumentList:
    """The employee's documents in the categories the reader may see (DOC-2)."""
    listing = await documents.employee_documents(
        actor, RequestContext.from_request(request), employee_id, include_archived=include_archived
    )
    return _listing(listing, documents)


@router.get("/company-documents", response_model=DocumentList)
async def company_documents(
    documents: Service,
    actor: Annotated[Actor, requires("document.company.read")],
    include_archived: bool = False,
) -> DocumentList:
    return _listing(await documents.company_documents(actor, include_archived=include_archived), documents)


@router.get("/documents/{document_id}", response_model=DocumentView)
async def document_detail(
    document_id: uuid.UUID,
    response: Response,
    documents: Service,
    actor: Annotated[Actor, requires("document.read")],
) -> DocumentView:
    return _document(await documents.detail(actor, document_id), response)


@router.post("/documents/{document_id}/versions/{version_id}/download", response_model=DocumentDownloadLink)
async def download_document(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    documents: Service,
    actor: Annotated[Actor, requires("document.read")],
) -> DocumentDownloadLink:
    """A link to the file valid for 60 seconds, once it passed the virus check. Every
    download is audited (DOC-3, DOC-4)."""
    url, seconds = await documents.download(
        actor, RequestContext.from_request(request), document_id, version_id
    )
    return DocumentDownloadLink(url=url, expires_in_seconds=seconds)


# --- uploading and changing -------------------------------------------------------------------


@router.post("/me/documents", response_model=DocumentView, status_code=status.HTTP_201_CREATED)
async def upload_my_document(
    *,
    request: Request,
    documents: Service,
    actor: Annotated[Actor, requires("document.upload.self")],
    category_id: uuid.UUID,
    title: Title,
    expires_on: date | None = None,
    file_name: FileName = None,
) -> DocumentView:
    """Add a document to your own record, in a category that allows it."""
    record = await documents.upload(
        actor,
        RequestContext.from_request(request),
        owner=None,
        own=True,
        category_id=category_id,
        title=title,
        expires_on=expires_on,
        upload=await _read_upload(request, file_name),
    )
    return _document(record)


@router.post(
    "/employees/{employee_id}/documents", response_model=DocumentView, status_code=status.HTTP_201_CREATED
)
async def upload_employee_document(
    *,
    employee_id: uuid.UUID,
    request: Request,
    documents: Service,
    actor: Annotated[Actor, requires("document.manage.all")],
    category_id: uuid.UUID,
    title: Title,
    expires_on: date | None = None,
    file_name: FileName = None,
) -> DocumentView:
    record = await documents.upload(
        actor,
        RequestContext.from_request(request),
        owner=employee_id,
        own=False,
        category_id=category_id,
        title=title,
        expires_on=expires_on,
        upload=await _read_upload(request, file_name),
    )
    return _document(record)


@router.post("/company-documents", response_model=DocumentView, status_code=status.HTTP_201_CREATED)
async def upload_company_document(
    *,
    request: Request,
    documents: Service,
    actor: Annotated[Actor, requires("document.company.manage")],
    category_id: uuid.UUID,
    title: Title,
    expires_on: date | None = None,
    file_name: FileName = None,
) -> DocumentView:
    record = await documents.upload(
        actor,
        RequestContext.from_request(request),
        owner=None,
        own=False,
        category_id=category_id,
        title=title,
        expires_on=expires_on,
        upload=await _read_upload(request, file_name),
    )
    return _document(record)


@router.post(
    "/documents/{document_id}/versions", response_model=DocumentView, status_code=status.HTTP_201_CREATED
)
async def upload_document_version(
    *,
    document_id: uuid.UUID,
    request: Request,
    documents: Service,
    actor: Annotated[Actor, requires("document.read")],
    file_name: FileName = None,
) -> DocumentView:
    """A new version of a document (DOC-6); earlier versions stay available."""
    record = await documents.add_version(
        actor, RequestContext.from_request(request), document_id, await _read_upload(request, file_name)
    )
    return _document(record)


@router.patch("/documents/{document_id}", response_model=DocumentView)
async def update_document(
    *,
    document_id: uuid.UUID,
    body: DocumentUpdate,
    request: Request,
    response: Response,
    documents: Service,
    actor: Annotated[Actor, requires("document.read")],
    if_match: IfMatch = None,
) -> DocumentView:
    """Change the title or expiry date, or archive or restore the document (HR)."""
    values = _sent(body)
    for name in ("title", "status"):
        if name in values and values[name] is None:
            raise field_error(name, "document.required", "Choose a value.")
    record = await documents.update(
        actor,
        RequestContext.from_request(request),
        document_id,
        expected_version=expected_version(if_match),
        values=values,
    )
    return _document(record, response)
