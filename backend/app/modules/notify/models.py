"""The email outbox, in-app notifications and notification preferences (docs/database-design.md
§4.9).

Outbox: one document per email to send. It names the recipient account and a template, never
an address or a secret: the worker resolves the address and builds any link token at send
time. Each message has an idempotency key, unique within its organization, so a retried
request never queues the same email twice. The status says whether it was actually sent
(`sent`, with `sent_at`), is waiting (`pending`, `sending`), failed or was cancelled.
"""

import dataclasses
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import Index, Record, allowed, asc, utcnow


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


@dataclasses.dataclass(kw_only=True, eq=False)
class EmailOutbox(Record):
    __collection__ = "email_outbox"
    __indexes__ = (
        Index(asc("organization_id", "idempotency_key"), name="uq_email_outbox_idempotency_key", unique=True),
        Index(asc("organization_id", "status", "next_attempt_at", "_id"), name="ix_email_outbox_due"),
        Index(asc("organization_id", "user_id", "template", "status"), name="ix_email_outbox_user_template"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "template": allowed(EmailTemplate),
        "template_data": {"maxProperties": 20},
        "idempotency_key": {"minLength": 1, "maxLength": 200},
        "status": allowed(OutboxStatus),
        "attempts": {"minimum": 0},
        "last_error": {"maxLength": 200},
    }
    __expr_rules__ = (
        {"$eq": [{"$eq": ["$status", "sent"]}, {"$ne": ["$sent_at", None]}]},
        {"$or": [{"$ne": ["$status", "sending"]}, {"$ne": ["$lease_expires_at", None]}]},
    )

    user_id: uuid.UUID
    template: str
    template_data: dict[str, Any] | None = None
    idempotency_key: str
    status: str = OutboxStatus.PENDING.value
    attempts: int = 0
    next_attempt_at: datetime = dataclasses.field(default_factory=utcnow)
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)
    sent_at: datetime | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class Notification(Record):
    """An in-app notification for one account. The title, body and link are display text built
    from non-sensitive values; nothing else is stored."""

    __collection__ = "notifications"
    __indexes__ = (
        Index(
            asc("organization_id", "idempotency_key"), name="uq_notifications_idempotency_key", unique=True
        ),
        Index(
            asc("organization_id", "recipient_user_id", "deleted_at", "created_at", "_id"),
            name="ix_notifications_recipient",
        ),
        Index(
            asc("organization_id", "recipient_user_id", "deleted_at", "read_at"),
            name="ix_notifications_unread",
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "type": {"maxLength": 100, "pattern": r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"},
        "subject_type": {"pattern": "^[a-z][a-z0-9_]{0,39}$"},
        "title": {"minLength": 1, "maxLength": 120},
        "body": {"minLength": 1, "maxLength": 300},
        "link": {"pattern": r"^/[A-Za-z0-9/_?=&.-]{0,199}$"},
        "idempotency_key": {"minLength": 1, "maxLength": 200},
    }
    __expr_rules__ = ({"$eq": [{"$eq": ["$subject_type", None]}, {"$eq": ["$subject_id", None]}]},)

    recipient_user_id: uuid.UUID
    type: str
    subject_type: str | None = None
    subject_id: uuid.UUID | None = None
    title: str
    body: str | None = None
    link: str | None = None
    idempotency_key: str
    read_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class NotificationPreference(Record):
    __collection__ = "notification_preferences"
    __indexes__ = (
        Index(
            asc("organization_id", "user_id", "type", "channel"),
            name="uq_notification_preferences_user_type_channel",
            unique=True,
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "type": {"minLength": 1, "maxLength": 100},
        "channel": allowed(("in_app", "email")),
    }

    user_id: uuid.UUID
    type: str
    channel: str
    enabled: bool
    updated_at: datetime = dataclasses.field(default_factory=utcnow)
