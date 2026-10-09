"""SQLAlchemy models for the `access` schema (docs/database-design.md §4.2, revisions 0008-0009, 0012).

The catalog tables are read-only for the runtime roles. `user_roles` rows are revoked, never
deleted, and cannot hold a derived role (database constraints and trigger). Grant requests
are history too: their limits are CHECK constraints and a trigger allows only forward status
changes and one-time decisions (revision 0012).
"""

import uuid
from datetime import datetime, timedelta
from enum import StrEnum

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
    __table_args__ = (
        UniqueConstraint("id", "is_derived"),
        UniqueConstraint("id", "key", name="uq_roles_id_key"),
        {"schema": SCHEMA},
    )

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
        Index(
            "uq_user_roles_grant_request_id",
            "grant_request_id",
            unique=True,
            postgresql_where=text("grant_request_id IS NOT NULL"),
        ),
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
    # The request (elevation, super admin assignment, break-glass) the assignment came from.
    grant_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access.role_grant_requests.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class GrantKind(StrEnum):
    ELEVATION = "elevation"
    SUPER_ADMIN_ASSIGNMENT = "super_admin_assignment"
    BREAK_GLASS = "break_glass"


class GrantStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"  # a super admin assignment: the role is held with no end
    REJECTED = "rejected"
    EXPIRED = "expired"  # not decided within the decision window
    ACTIVE = "active"  # an elevation or break-glass inside its window
    ENDED = "ended"  # its window is over
    REVOKED = "revoked"  # withdrawn while pending, or ended early


class RoleGrantRequest(Base):
    __tablename__ = "role_grant_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["role_id", "role_key"], ["access.roles.id", "access.roles.key"], ondelete="RESTRICT"
        ),
        Index(
            "uq_role_grant_requests_pending",
            "subject_user_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_role_grant_requests_active",
            "subject_user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_role_grant_requests_status",
            "status",
            postgresql_where=text("status IN ('pending', 'active')"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    kind: Mapped[str]
    subject_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    role_id: Mapped[uuid.UUID]
    role_key: Mapped[str]
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org.departments.id", ondelete="RESTRICT")
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org.locations.id", ondelete="RESTRICT"))
    reason: Mapped[str]
    requested_duration: Mapped[timedelta | None]
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[str]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None]
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    revoked_at: Mapped[datetime | None]
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("identity.users.id", ondelete="RESTRICT")
    )
    acknowledged_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
