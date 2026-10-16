"""SQLAlchemy models for the `docs` schema (docs/database-design.md §4.8, revision 0022).

Versions are append-only except their scan result, recorded once (trigger
`docs.guard_versions`). Allowed values are CHECK constraints in the migration; the enums
mirror them.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, Index, LargeBinary, SmallInteger, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "docs"


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


class DocumentCategory(Base):
    __tablename__ = "document_categories"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    code: Mapped[str] = mapped_column(CITEXT(), unique=True)
    name: Mapped[str]
    description: Mapped[str | None]
    sensitivity: Mapped[str]
    employee_visible: Mapped[bool]
    employee_can_upload: Mapped[bool]
    requires_expiry: Mapped[bool]
    status: Mapped[str] = mapped_column(server_default=Status.ACTIVE.value)
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_owner_employee_id_category_id", "owner_employee_id", "category_id"),
        Index("ix_documents_category_id", "category_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    # None for a company document.
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.employees.id", ondelete="RESTRICT")
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("docs.document_categories.id", ondelete="RESTRICT")
    )
    title: Mapped[str]
    expires_on: Mapped[date | None]
    status: Mapped[str] = mapped_column(server_default=Status.ACTIVE.value)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no"),
        Index(
            "ix_document_versions_pending", "uploaded_at", postgresql_where=text("scan_status = 'pending'")
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("docs.documents.id", ondelete="RESTRICT"))
    version_no: Mapped[int]
    # A random key under `documents/`; never anything a person typed.
    storage_key: Mapped[str] = mapped_column(unique=True)
    original_filename: Mapped[str]
    # Decided from the file's content, not its name.
    content_type: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger())
    sha256: Mapped[bytes] = mapped_column(LargeBinary())
    scan_status: Mapped[str] = mapped_column(server_default=ScanStatus.PENDING.value)
    scan_attempts: Mapped[int] = mapped_column(SmallInteger(), server_default=text("0"))
    scanned_at: Mapped[datetime | None]
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now())
