"""SQLAlchemy models for the `access` schema (docs/database-design.md §4.2, revisions 0008-0009).

The catalog tables are read-only for the runtime roles. `user_roles` rows are revoked, never
deleted, and cannot hold a derived role (database constraints and trigger).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "access"


class PermissionRow(Base):
    __tablename__ = "permissions"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    key: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str]
    requires_step_up: Mapped[bool]
    audit_reads: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("id", "is_derived"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    key: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    description: Mapped[str]
    is_system: Mapped[bool]
    is_derived: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (Index("ix_role_permissions_permission_id", "permission_id"), {"schema": SCHEMA})

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access.roles.id", ondelete="RESTRICT"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access.permissions.id", ondelete="RESTRICT"), primary_key=True
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["role_id", "role_is_derived"],
            ["access.roles.id", "access.roles.is_derived"],
            ondelete="RESTRICT",
        ),
        Index(
            "uq_user_roles_active_assignment",
            "user_id",
            "role_id",
            text("coalesce(department_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            text("coalesce(location_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_user_roles_user_id", "user_id", postgresql_where=text("revoked_at IS NULL")),
        Index("ix_user_roles_role_id", "role_id", postgresql_where=text("revoked_at IS NULL")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    role_id: Mapped[uuid.UUID]
    role_is_derived: Mapped[bool] = mapped_column(Boolean(), server_default=text("false"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org.departments.id", ondelete="RESTRICT")
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org.locations.id", ondelete="RESTRICT"))
    valid_from: Mapped[datetime] = mapped_column(server_default=func.now())
    valid_until: Mapped[datetime | None]
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    grant_reason: Mapped[str]
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
