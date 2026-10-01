"""SQLAlchemy models for the `org` schema (docs/database-design.md §4.3, revision 0003).

Length limits and allowed values are CHECK constraints in the migration; the enums below
mirror the allowed values for code that reads these tables.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CHAR, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "org"


class OrgUnitStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    code: Mapped[str] = mapped_column(CITEXT(), unique=True)
    name: Mapped[str]
    # IANA zone name; validated by the service that writes locations (M2).
    time_zone: Mapped[str]
    country_code: Mapped[str] = mapped_column(CHAR(2))
    address: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=OrgUnitStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        Index("ix_departments_parent_id", "parent_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    code: Mapped[str] = mapped_column(CITEXT(), unique=True)
    name: Mapped[str]
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org.departments.id", ondelete="RESTRICT"))
    # Foreign key to people.employees; added by revision 0004 because that table is created there.
    head_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.employees.id", ondelete="RESTRICT", use_alter=True)
    )
    status: Mapped[str] = mapped_column(server_default=OrgUnitStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Designation(Base):
    __tablename__ = "designations"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    code: Mapped[str] = mapped_column(CITEXT(), unique=True)
    name: Mapped[str]
    level: Mapped[int | None]
    status: Mapped[str] = mapped_column(server_default=OrgUnitStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
