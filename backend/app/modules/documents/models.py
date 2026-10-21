"""Documents (docs/database-design.md §4.8): categories, documents and their versions.

A version's file lives in GridFS under its `storage_key`; the version document holds the
metadata. Versions are never deleted and change only to record the scan result, once (formerly
a guard trigger): `scan_status`, `scanned_at` and `scan_attempts` while it is pending. Allowed
values and the rules within one document are part of each collection's validator.
"""

import dataclasses
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import CASE_INSENSITIVE, Index, Record, allowed, asc, utcnow


class Sensitivity(StrEnum):
    INTERNAL = "internal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


class Status(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ScanStatus(StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    FAILED = "failed"


@dataclasses.dataclass(kw_only=True, eq=False)
class DocumentCategory(Record):
    __collection__ = "document_categories"
    __deletable__ = False
    __indexes__ = (
        Index(
            asc("organization_id", "code"),
            name="uq_document_categories_code",
            unique=True,
            collation=CASE_INSENSITIVE,
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "code": {"minLength": 1, "maxLength": 32, "pattern": r"^\S+$"},
        "name": {"minLength": 1, "maxLength": 200},
        "description": {"minLength": 1, "maxLength": 1000},
        "sensitivity": allowed(Sensitivity),
        "status": allowed(Status),
        "version": {"minimum": 1},
    }
    __expr_rules__ = ({"$or": ["$employee_visible", {"$not": ["$employee_can_upload"]}]},)

    code: str
    name: str
    description: str | None = None
    sensitivity: str
    employee_visible: bool
    employee_can_upload: bool
    requires_expiry: bool
    status: str = Status.ACTIVE.value
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class Document(Record):
    __collection__ = "documents"
    __deletable__ = False
    __indexes__ = (
        Index(asc("organization_id", "owner_employee_id", "category_id"), name="ix_documents_owner_category"),
        Index(asc("organization_id", "category_id"), name="ix_documents_category_id"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "title": {"minLength": 1, "maxLength": 200},
        "status": allowed(Status),
        "version": {"minimum": 1},
    }

    # None for a company document.
    owner_employee_id: uuid.UUID | None = None
    category_id: uuid.UUID
    title: str
    expires_on: date | None = None
    status: str = Status.ACTIVE.value
    created_by: uuid.UUID
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class DocumentVersion(Record):
    __collection__ = "document_versions"
    __deletable__ = False
    __mutable__ = frozenset({"scan_status", "scanned_at", "scan_attempts"})
    __set_once__ = frozenset({"scanned_at"})
    __transitions__: ClassVar[dict[str, dict[str, frozenset[str]]]] = {
        "scan_status": {"pending": frozenset({"clean", "infected", "failed"})},
    }
    __indexes__ = (
        Index(
            asc("organization_id", "document_id", "version_no"),
            name="uq_document_versions_version_no",
            unique=True,
        ),
        Index(asc("storage_key"), name="uq_document_versions_storage_key", unique=True),
        Index(asc("organization_id", "scan_status", "uploaded_at"), name="ix_document_versions_pending"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "version_no": {"minimum": 1},
        "storage_key": {"pattern": "^documents/[0-9a-f-]{36}$"},
        "original_filename": {"minLength": 1, "maxLength": 255},
        "content_type": allowed(("application/pdf", "image/png", "image/jpeg")),
        "size_bytes": {"minimum": 1, "maximum": 10485760},
        "scan_status": allowed(ScanStatus),
        "scan_attempts": {"minimum": 0, "maximum": 100},
    }
    __expr_rules__ = ({"$eq": [{"$eq": ["$scan_status", "pending"]}, {"$eq": ["$scanned_at", None]}]},)

    document_id: uuid.UUID
    version_no: int
    # A random key under `documents/`; never anything a person typed.
    storage_key: str
    original_filename: str
    # Decided from the file's content, not its name.
    content_type: str
    size_bytes: int
    sha256: bytes
    scan_status: str = ScanStatus.PENDING.value
    scan_attempts: int = 0
    scanned_at: datetime | None = None
    uploaded_by: uuid.UUID
    uploaded_at: datetime = dataclasses.field(default_factory=utcnow)
