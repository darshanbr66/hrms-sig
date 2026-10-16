"""SQLAlchemy models for the `notify` schema (docs/database-design.md §4.9): the email outbox
(revision 0010), in-app notifications and notification preferences (revision 0019).

Outbox: one row per email to send. The row names the recipient account and a template, never an
address or a secret: the worker resolves the address and builds any link token at send time.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, SmallInteger, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "notify"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EmailTemplate(StrEnum):
    INVITE = "identity.invite"
    PASSWORD_RESET = "identity.password_reset"  # noqa: S105 (a template name, not a secret)
    PASSWORD_CHANGED = "identity.password_changed"  # noqa: S105 (a template name, not a secret)
    NEW_DEVICE = "identity.new_device"
    ACCOUNT_LOCKED = "identity.account_locked"
    RECOVERY_CODE_USED = "identity.recovery_code_used"
    MFA_CHANGED = "identity.mfa_changed"
    MFA_RESET = "identity.mfa_reset"
    GRANT_REQUEST = "access.grant_request"
    AUDIT_CHAIN_MISMATCH = "audit.chain_mismatch"
    LEAVE_REQUEST = "leave.request"
    ATTENDANCE_CORRECTION = "attendance.correction"


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    __table_args__ = (
        Index(
            "ix_email_outbox_next_attempt_at",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'sending')"),
        ),
        Index(
            "ix_email_outbox_user_id_template",
            "user_id",
            "template",
            postgresql_where=text("status = 'pending'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    template: Mapped[str]
    template_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger(), server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    lease_expires_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]


class Notification(Base):
    """An in-app notification for one account (revision 0019). The title, body and link are
    display text built from non-sensitive values; nothing else is stored."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "ix_notifications_recipient_user_id_created_at",
            "recipient_user_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_notifications_recipient_user_id_unread",
            "recipient_user_id",
            postgresql_where=text("deleted_at IS NULL AND read_at IS NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    type: Mapped[str]
    subject_type: Mapped[str | None]
    subject_id: Mapped[uuid.UUID | None]
    title: Mapped[str]
    body: Mapped[str | None]
    link: Mapped[str | None]
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    read_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("user_id", "type", "channel"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identity.users.id", ondelete="RESTRICT"))
    type: Mapped[str]
    channel: Mapped[str]
    enabled: Mapped[bool]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
