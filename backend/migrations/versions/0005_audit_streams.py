"""Audit streams: audit_log and security_events (docs/security-architecture.md §8).

Revision ID: 0005
Revises: 0004

Expand/contract: new tables and functions only.

- Both streams are partitioned by UTC month on `recorded_at`, the database insert time.
  A trigger rejects any other value, so rows cannot be back- or forward-dated into another
  month's partition (which the sealer would otherwise have to treat as tampering). The same
  trigger assigns `id` itself, overwriting any supplied value, so record IDs are unique
  across partitions and the sealer can key chain links by record ID.
- Append-only: the runtime roles get INSERT on the parent tables only, never on a
  partition, so every row passes through routing and the triggers. They read through the
  schema's default SELECT grant. UPDATE, DELETE and TRUNCATE are never granted. Triggers
  reject UPDATE, DELETE and TRUNCATE for every role, including the owner, as a second line
  of defence. TRUNCATE triggers do not propagate to partitions, so each partition gets its
  own.
- `audit.ensure_partitions(months_ahead)` creates missing month partitions. It runs as the
  owner (SECURITY DEFINER) because only the owner can attach partitions; it takes one integer,
  builds every identifier itself, and only the worker may execute it (hourly job). A new
  partition is created detached and then attached, which does not block concurrent inserts.
- There are no foreign keys from the audit tables: audit rows record denials against targets
  that may not exist, and they outlive the rows they describe (retention, anonymization).

The hash-chain tables (chain_links, chain_checkpoints) arrive with the sealer in
Checkpoint F.
"""

from collections.abc import Sequence

from alembic import op

from migrations.support import APP_ROLE, WORKER_ROLE, quote_ident

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INITIAL_MONTHS_AHEAD = 3

# Dotted lower-case names: `leave.request.approved`, `login.failed`.
DOTTED_NAME = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
# <domain>.<resource>.<action>[.<scope>] (docs/authorization-model.md §2).
PERMISSION_KEY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,3}$"
MAX_JSON_BYTES = 16384


def upgrade() -> None:
    writers = f"{quote_ident(APP_ROLE)}, {quote_ident(WORKER_ROLE)}"

    op.execute(
        f"""
        CREATE TABLE audit.audit_log (
            id uuid NOT NULL DEFAULT uuidv7(),
            recorded_at timestamptz NOT NULL DEFAULT now(),
            occurred_at timestamptz NOT NULL,
            actor_type text NOT NULL,
            actor_user_id uuid,
            session_id uuid,
            grant_request_id uuid,
            request_id uuid,
            ip inet,
            user_agent text,
            action text NOT NULL,
            permission_used text,
            target_type text,
            target_id text,
            subject_employee_id uuid,
            outcome text NOT NULL,
            changes jsonb,
            reason text,
            CONSTRAINT pk_audit_log PRIMARY KEY (id, recorded_at),
            CONSTRAINT ck_audit_log_actor_type CHECK (actor_type IN ('user', 'system', 'job')),
            CONSTRAINT ck_audit_log_user_actor CHECK ((actor_type = 'user') = (actor_user_id IS NOT NULL)),
            CONSTRAINT ck_audit_log_session_needs_user CHECK (session_id IS NULL OR actor_type = 'user'),
            CONSTRAINT ck_audit_log_grant_needs_user CHECK (grant_request_id IS NULL OR actor_type = 'user'),
            CONSTRAINT ck_audit_log_user_agent CHECK (char_length(user_agent) <= 512),
            CONSTRAINT ck_audit_log_action CHECK (char_length(action) <= 100 AND action ~ '{DOTTED_NAME}'),
            CONSTRAINT ck_audit_log_permission_used CHECK (
                char_length(permission_used) <= 100 AND permission_used ~ '{PERMISSION_KEY}'
            ),
            CONSTRAINT ck_audit_log_target CHECK ((target_type IS NULL) = (target_id IS NULL)),
            CONSTRAINT ck_audit_log_target_type CHECK (
                char_length(target_type) <= 64 AND target_type ~ '^[a-z][a-z0-9_]*$'
            ),
            CONSTRAINT ck_audit_log_target_id CHECK (char_length(target_id) BETWEEN 1 AND 200),
            CONSTRAINT ck_audit_log_outcome CHECK (outcome IN ('success', 'denied', 'failed')),
            CONSTRAINT ck_audit_log_changes CHECK (
                jsonb_typeof(changes) = 'object' AND octet_length(changes::text) <= {MAX_JSON_BYTES}
            ),
            CONSTRAINT ck_audit_log_reason CHECK (char_length(reason) BETWEEN 1 AND 1000)
        ) PARTITION BY RANGE (recorded_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_audit_log_subject_employee_id_recorded_at "
        "ON audit.audit_log (subject_employee_id, recorded_at) WHERE subject_employee_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_actor_user_id_recorded_at "
        "ON audit.audit_log (actor_user_id, recorded_at) WHERE actor_user_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_audit_log_action_recorded_at ON audit.audit_log (action, recorded_at)")
    op.execute(
        "CREATE INDEX ix_audit_log_grant_request_id "
        "ON audit.audit_log (grant_request_id) WHERE grant_request_id IS NOT NULL"
    )

    op.execute(
        f"""
        CREATE TABLE audit.security_events (
            id uuid NOT NULL DEFAULT uuidv7(),
            recorded_at timestamptz NOT NULL DEFAULT now(),
            occurred_at timestamptz NOT NULL,
            event_type text NOT NULL,
            severity text NOT NULL,
            user_id uuid,
            session_id uuid,
            request_id uuid,
            email_attempted_hash bytea,
            ip inet,
            user_agent text,
            details jsonb,
            CONSTRAINT pk_security_events PRIMARY KEY (id, recorded_at),
            CONSTRAINT ck_security_events_event_type CHECK (
                char_length(event_type) <= 100 AND event_type ~ '{DOTTED_NAME}'
            ),
            CONSTRAINT ck_security_events_severity CHECK (severity IN ('info', 'warning', 'high')),
            CONSTRAINT ck_security_events_email_attempted_hash CHECK (
                octet_length(email_attempted_hash) = 32
            ),
            CONSTRAINT ck_security_events_user_agent CHECK (char_length(user_agent) <= 512),
            CONSTRAINT ck_security_events_details CHECK (
                jsonb_typeof(details) = 'object' AND octet_length(details::text) <= {MAX_JSON_BYTES}
            )
        ) PARTITION BY RANGE (recorded_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_security_events_user_id_recorded_at "
        "ON audit.security_events (user_id, recorded_at) WHERE user_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_security_events_event_type_recorded_at "
        "ON audit.security_events (event_type, recorded_at)"
    )

    op.execute(
        """
        CREATE FUNCTION audit.reject_modification() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'audit records are append-only: % is not allowed on audit.%', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION audit.assign_record_keys() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.recorded_at IS DISTINCT FROM now() THEN
                RAISE EXCEPTION 'audit.%.recorded_at is set by the database', TG_TABLE_NAME
                    USING ERRCODE = 'check_violation';
            END IF;
            NEW.id := uuidv7();
            RETURN NEW;
        END
        $$
        """
    )
    for table in ("audit_log", "security_events"):
        op.execute(
            f"CREATE TRIGGER {table}_record_keys BEFORE INSERT ON audit.{table} "
            "FOR EACH ROW EXECUTE FUNCTION audit.assign_record_keys()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON audit.{table} "
            "FOR EACH ROW EXECUTE FUNCTION audit.reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON audit.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_modification()"
        )

    op.execute(
        """
        CREATE FUNCTION audit.ensure_partitions(months_ahead integer) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        SET "TimeZone" = 'UTC'
        SET lock_timeout = '5s'
        AS $$
        DECLARE
            first_month date := date_trunc('month', now())::date;
            month_start date;
            partition_name text;
            parent text;
            created integer := 0;
        BEGIN
            IF months_ahead IS NULL OR months_ahead < 0 OR months_ahead > 12 THEN
                RAISE EXCEPTION 'months_ahead must be between 0 and 12';
            END IF;
            -- One caller at a time; a second caller waits and then finds nothing to do.
            PERFORM pg_advisory_xact_lock(hashtextextended('audit.ensure_partitions', 0));
            FOREACH parent IN ARRAY ARRAY['audit_log', 'security_events'] LOOP
                FOR offset_months IN 0..months_ahead LOOP
                    month_start := (first_month + make_interval(months => offset_months))::date;
                    partition_name := format('%s_p%s', parent, to_char(month_start, 'YYYY_MM'));
                    IF to_regclass(format('audit.%I', partition_name)) IS NOT NULL THEN
                        -- A same-named table that is not attached would leave the month without a
                        -- partition while this job reports success. Fail loudly; never attach a
                        -- table whose contents this function did not create.
                        IF NOT EXISTS (
                            SELECT FROM pg_inherits
                            WHERE inhrelid = to_regclass(format('audit.%I', partition_name))
                              AND inhparent = to_regclass(format('audit.%I', parent))
                        ) THEN
                            RAISE EXCEPTION 'audit.% exists but is not a partition of audit.%',
                                partition_name, parent;
                        END IF;
                        CONTINUE;
                    END IF;
                    EXECUTE format(
                        'CREATE TABLE audit.%I (LIKE audit.%I INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
                        partition_name, parent
                    );
                    EXECUTE format(
                        'ALTER TABLE audit.%I ATTACH PARTITION audit.%I FOR VALUES FROM (%L) TO (%L)',
                        parent, partition_name,
                        month_start::timestamp AT TIME ZONE 'UTC',
                        (month_start + interval '1 month')::timestamp AT TIME ZONE 'UTC'
                    );
                    EXECUTE format(
                        'CREATE TRIGGER %I BEFORE TRUNCATE ON audit.%I '
                        'FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_modification()',
                        partition_name || '_no_truncate', partition_name
                    );
                    created := created + 1;
                END LOOP;
            END LOOP;
            RETURN created;
        END
        $$
        """
    )
    op.execute(f"SELECT audit.ensure_partitions({INITIAL_MONTHS_AHEAD})")

    op.execute(f"GRANT INSERT ON audit.audit_log, audit.security_events TO {writers}")
    op.execute(f"GRANT EXECUTE ON FUNCTION audit.ensure_partitions(integer) TO {quote_ident(WORKER_ROLE)}")


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP FUNCTION audit.ensure_partitions(integer)")
    op.execute("DROP TABLE audit.security_events")
    op.execute("DROP TABLE audit.audit_log")
    op.execute("DROP FUNCTION audit.assign_record_keys()")
    op.execute("DROP FUNCTION audit.reject_modification()")
