"""Table definitions for the audit streams (revision 0005).

Both tables are partitioned by UTC month on `recorded_at`; the database sets `id` and
`recorded_at`, and rejects UPDATE, DELETE and TRUNCATE. CHECK constraints live in the
migration.
"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    Table,
    Text,
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
