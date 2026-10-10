"""Table definitions for the audit streams (revision 0005) and their integrity chain (0013).

Both streams are partitioned by UTC month on `recorded_at`; the database sets `id` and
`recorded_at`, and rejects UPDATE, DELETE and TRUNCATE. The chain tables are insert-only too:
`chain_links` is partitioned by stream and then by month, `chain_checkpoints` is one table
that is never deleted from. CHECK constraints and triggers live in the migrations.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB

from app.platform.db import Base

SCHEMA = "audit"

audit_log = Table(
    "audit_log",
    Base.metadata,
    Column("id", Uuid(), nullable=False, server_default=text("uuidv7()")),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("actor_type", Text(), nullable=False),
    Column("actor_user_id", Uuid()),
    Column("session_id", Uuid()),
    Column("grant_request_id", Uuid()),
    Column("request_id", Uuid()),
    Column("ip", INET()),
    Column("user_agent", Text()),
    Column("action", Text(), nullable=False),
    Column("permission_used", Text()),
    Column("target_type", Text()),
    Column("target_id", Text()),
    Column("subject_employee_id", Uuid()),
    Column("outcome", Text(), nullable=False),
    # none_as_null: Python None is SQL NULL, not a JSON null value.
    Column("changes", JSONB(none_as_null=True)),
    Column("reason", Text()),
    PrimaryKeyConstraint("id", "recorded_at", name="pk_audit_log"),
    Index(
        "ix_audit_log_subject_employee_id_recorded_at",
        "subject_employee_id",
        "recorded_at",
        postgresql_where=text("subject_employee_id IS NOT NULL"),
    ),
    Index(
        "ix_audit_log_actor_user_id_recorded_at",
        "actor_user_id",
        "recorded_at",
        postgresql_where=text("actor_user_id IS NOT NULL"),
    ),
    Index("ix_audit_log_action_recorded_at", "action", "recorded_at"),
    Index(
        "ix_audit_log_grant_request_id",
        "grant_request_id",
        postgresql_where=text("grant_request_id IS NOT NULL"),
    ),
    schema=SCHEMA,
    postgresql_partition_by="RANGE (recorded_at)",
)

security_events = Table(
    "security_events",
    Base.metadata,
    Column("id", Uuid(), nullable=False, server_default=text("uuidv7()")),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("event_type", Text(), nullable=False),
    Column("severity", Text(), nullable=False),
    Column("user_id", Uuid()),
    Column("session_id", Uuid()),
    Column("request_id", Uuid()),
    Column("email_attempted_hash", LargeBinary()),
    Column("ip", INET()),
    Column("user_agent", Text()),
    Column("details", JSONB(none_as_null=True)),
    PrimaryKeyConstraint("id", "recorded_at", name="pk_security_events"),
    Index(
        "ix_security_events_user_id_recorded_at",
        "user_id",
        "recorded_at",
        postgresql_where=text("user_id IS NOT NULL"),
    ),
    Index("ix_security_events_event_type_recorded_at", "event_type", "recorded_at"),
    schema=SCHEMA,
    postgresql_partition_by="RANGE (recorded_at)",
)

chain_links = Table(
    "chain_links",
    Base.metadata,
    Column("stream", Text(), nullable=False),
    Column("partition_key", Text(), nullable=False),
    Column("position", BigInteger(), nullable=False),
    Column("record_id", Uuid(), nullable=False),
    Column("digest_version", SmallInteger(), nullable=False),
    Column("row_digest", LargeBinary(), nullable=False),
    Column("link_hash", LargeBinary(), nullable=False),
    Column("sealed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("stream", "partition_key", "position", name="pk_chain_links"),
    UniqueConstraint("stream", "partition_key", "record_id"),
    schema=SCHEMA,
    postgresql_partition_by="LIST (stream)",
)

chain_checkpoints = Table(
    "chain_checkpoints",
    Base.metadata,
    Column("stream", Text(), nullable=False),
    Column("checkpoint_no", BigInteger(), nullable=False),
    Column("kind", Text(), nullable=False),
    Column("partition_key", Text(), nullable=False),
    Column("last_position", BigInteger(), nullable=False),
    Column("row_count", BigInteger(), nullable=False),
    Column("last_link_hash", LargeBinary(), nullable=False),
    Column("prev_checkpoint_hash", LargeBinary(), nullable=False),
    Column("checkpoint_hash", LargeBinary(), nullable=False),
    Column("signature", LargeBinary(), nullable=False),
    Column("key_id", Text(), nullable=False),
    Column("anchor_object_key", Text(), nullable=False),
    Column("archive_sha256", LargeBinary()),
    Column("retention_policy_version", Text()),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("stream", "checkpoint_no", name="pk_chain_checkpoints"),
    Index(
        "uq_chain_checkpoints_partition_final",
        "stream",
        "partition_key",
        unique=True,
        postgresql_where=text("kind = 'partition_final'"),
    ),
    Index(
        "uq_chain_checkpoints_retention_tombstone",
        "stream",
        "partition_key",
        unique=True,
        postgresql_where=text("kind = 'retention_tombstone'"),
    ),
    schema=SCHEMA,
)
