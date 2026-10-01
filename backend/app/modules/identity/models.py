"""SQLAlchemy models for the `identity` schema (docs/database-design.md §4.1, revision 0007).

Length limits, allowed values and the MFA invariant are enforced by the migration; the enums
below mirror the allowed values. No model here is ever serialized directly into a response.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, Index, LargeBinary, SmallInteger, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "identity"


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


class FactorType(StrEnum):
    TOTP = "totp"


class ClientType(StrEnum):
    WEB = "web"


class RevokedReason(StrEnum):
    LOGOUT = "logout"
    REVOKED_BY_USER = "revoked_by_user"
    REVOKED_BY_ADMIN = "revoked_by_admin"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 (a reason name, not a secret)
    ACCOUNT_DISABLED = "account_disabled"
    TOKEN_REUSE = "token_reuse"  # noqa: S105 (a reason name, not a secret)


class TokenKind(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class OneTimePurpose(StrEnum):
    INVITE = "invite"
    INVITE_ENROLMENT = "invite_enrolment"
    LOGIN_MFA = "login_mfa"


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    email: Mapped[str] = mapped_column(CITEXT(), unique=True)
    email_verified_at: Mapped[datetime | None]
    status: Mapped[str]
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.employees.id", ondelete="RESTRICT"), unique=True
    )
    last_login_at: Mapped[datetime | None]
    failed_login_count: Mapped[int] = mapped_column(SmallInteger(), server_default=text("0"))
    failed_login_window_started_at: Mapped[datetime | None]
    last_failed_login_at: Mapped[datetime | None]
    locked_until: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity.users.id", ondelete="RESTRICT"), primary_key=True
    )
    password_hash: Mapped[str]
    password_changed_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MfaFactor(Base):
    __tablename__ = "mfa_factors"
    __table_args__ = (
        Index("ix_mfa_factors_user_id", "user_id", postgresql_where=text("revoked_at IS NULL")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    type: Mapped[str]
    label: Mapped[str]
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary())
    secret_key_version: Mapped[int] = mapped_column(SmallInteger())
    last_used_step: Mapped[int | None] = mapped_column(BigInteger())
    last_used_at: Mapped[datetime | None]
    confirmed_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"
    __table_args__ = (UniqueConstraint("user_id", "code_hash"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    code_hash: Mapped[bytes] = mapped_column(LargeBinary())
    used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuthSession(Base):
    """A signed-in session (`identity.sessions`)."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id", postgresql_where=text("revoked_at IS NULL")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    client_type: Mapped[str]
    scope: Mapped[str]
    ip: Mapped[str | None] = mapped_column(INET())
    user_agent: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_activity_at: Mapped[datetime]
    idle_expires_at: Mapped[datetime]
    absolute_expires_at: Mapped[datetime]
    step_up_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    revoked_reason: Mapped[str | None]


class SessionToken(Base):
    __tablename__ = "session_tokens"
    __table_args__ = (Index("ix_session_tokens_session_id", "session_id"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.sessions.id", ondelete="CASCADE"))
    kind: Mapped[str]
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("identity.session_tokens.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OneTimeToken(Base):
    __tablename__ = "one_time_tokens"
    __table_args__ = (Index("ix_one_time_tokens_user_id", "user_id"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    purpose: Mapped[str]
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    failed_attempts: Mapped[int] = mapped_column(SmallInteger(), server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
