"""Collections of the audit streams and their integrity chain (docs/security-architecture.md §8).

All four are append-only: the unit of work refuses updates and deletes, and only the audit
retention database user may remove records (after a signed tombstone, `retention.py`). The
writer sets `id`, `recorded_at` and `month` (the UTC month of `recorded_at`, the chain's
partition key).

Every business audit record belongs to an organization. A security event belongs to the
organization of the account it concerns, or to none when there is no such account (a sign-in
attempt with an unknown email address). The chain is one per stream and month across all
organizations, so a deleted or changed record of any organization is detected.
"""

import dataclasses
import uuid
from datetime import datetime
from typing import Any, ClassVar

from app.platform.db import Document, Index, asc, utcnow
from app.platform.ids import uuid7


@dataclasses.dataclass(kw_only=True, eq=False)
class AuditLogRecord(Document):
    __collection__ = "audit_log"
    __append_only__ = True
    __indexes__ = (
        Index(asc("organization_id", "subject_employee_id", "recorded_at"), name="ix_audit_log_subject"),
        Index(asc("organization_id", "actor_user_id", "recorded_at"), name="ix_audit_log_actor"),
        Index(asc("organization_id", "action", "recorded_at"), name="ix_audit_log_action"),
        Index(asc("organization_id", "recorded_at"), name="ix_audit_log_recorded_at"),
        Index(asc("organization_id", "grant_request_id"), name="ix_audit_log_grant_request"),
        Index(asc("month", "recorded_at", "_id"), name="ix_audit_log_month"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "action": {"maxLength": 100},
        "month": {"pattern": r"^\d{4}-\d{2}$"},
        "user_agent": {"maxLength": 512},
        "reason": {"maxLength": 2000},
    }

    id: uuid.UUID = dataclasses.field(default_factory=uuid7)
    organization_id: uuid.UUID | None = None
    recorded_at: datetime = dataclasses.field(default_factory=utcnow)
    month: str = ""
    occurred_at: datetime
    actor_type: str
    actor_user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    grant_request_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    ip: str | None = None
    user_agent: str | None = None
    action: str
    permission_used: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    subject_employee_id: uuid.UUID | None = None
    outcome: str
    changes: dict[str, Any] | None = None
    reason: str | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class SecurityEventRecord(Document):
    __collection__ = "security_events"
    __append_only__ = True
    # Written with the organization of the session when there is one (writer.py); read by the
    # audit module with an explicit organization condition.
    __tenant_scoped__ = False
    __indexes__ = (
        Index(asc("organization_id", "user_id", "recorded_at"), name="ix_security_events_user"),
        Index(asc("organization_id", "event_type", "recorded_at"), name="ix_security_events_type"),
        Index(asc("organization_id", "recorded_at"), name="ix_security_events_recorded_at"),
        Index(asc("month", "recorded_at", "_id"), name="ix_security_events_month"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "event_type": {"maxLength": 100},
        "month": {"pattern": r"^\d{4}-\d{2}$"},
        "user_agent": {"maxLength": 512},
    }

    id: uuid.UUID = dataclasses.field(default_factory=uuid7)
    organization_id: uuid.UUID | None = None
    recorded_at: datetime = dataclasses.field(default_factory=utcnow)
    month: str = ""
    occurred_at: datetime
    event_type: str
    severity: str
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    email_attempted_hash: bytes | None = None
    ip: str | None = None
    user_agent: str | None = None
    details: dict[str, Any] | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class ChainLink(Document):
    """`audit_chain_links`: one sealed record's place in its stream's monthly chain."""

    __collection__ = "audit_chain_links"
    __tenant_scoped__ = False
    __append_only__ = True
    __indexes__ = (
        Index(asc("stream", "partition_key", "position"), name="uq_audit_chain_links_position", unique=True),
        Index(asc("stream", "partition_key", "record_id"), name="uq_audit_chain_links_record", unique=True),
    )

    id: uuid.UUID = dataclasses.field(default_factory=uuid7)
    stream: str
    partition_key: str
    position: int
    record_id: uuid.UUID
    digest_version: int
    row_digest: bytes
    link_hash: bytes
    sealed_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class ChainCheckpoint(Document):
    """`audit_checkpoints`: the signed checkpoint chain of a stream."""

    __collection__ = "audit_checkpoints"
    __tenant_scoped__ = False
    __append_only__ = True
    __indexes__ = (
        Index(asc("stream", "checkpoint_no"), name="uq_audit_checkpoints_number", unique=True),
        Index(
            asc("stream", "partition_key"),
            name="uq_audit_checkpoints_partition_final",
            unique=True,
            partial={"kind": "partition_final"},
        ),
        Index(
            asc("stream", "partition_key"),
            name="uq_audit_checkpoints_retention_tombstone",
            unique=True,
            partial={"kind": "retention_tombstone"},
        ),
    )

    id: uuid.UUID = dataclasses.field(default_factory=uuid7)
    stream: str
    checkpoint_no: int
    kind: str
    partition_key: str
    last_position: int
    row_count: int
    last_link_hash: bytes
    prev_checkpoint_hash: bytes
    checkpoint_hash: bytes
    signature: bytes
    key_id: str
    archive_sha256: bytes | None = None
    retention_policy_version: str | None = None
    created_at: datetime


@dataclasses.dataclass(kw_only=True, eq=False)
class AuditLock(Document):
    """`audit_locks`: one document per lock name. Writers of a stream's chain write it first in
    their transaction, so a second writer meets a write conflict instead of interleaving."""

    __collection__ = "audit_locks"
    __id_field__ = "name"
    __tenant_scoped__ = False

    name: str
    taken: int = 0
