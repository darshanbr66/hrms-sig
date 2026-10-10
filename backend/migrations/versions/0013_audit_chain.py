"""Audit integrity: chain links, signed checkpoints, partition removal (security-architecture.md §8.1).

Revision ID: 0013
Revises: 0012

Expand/contract: new tables and functions; one function body replaced (CREATE OR REPLACE
keeps its owner, SECURITY DEFINER and grants); one CHECK constraint widened. No existing
table is rewritten. Lock impact: the outbox CHECK is re-validated (a scan of a small table);
`audit.ensure_partitions` creates the chain partitions of every existing audit partition,
each attached while empty.

- `audit.chain_links` is the sealer's row chain: one row per sealed audit record, keyed by
  `(stream, partition_key, position)`. It is partitioned by stream and then by month, so a
  month's links are removed together with the audited partition they cover (retention), and
  each stream keeps its own retention. PostgreSQL requires the partition columns in every
  unique key, so record uniqueness is `(stream, partition_key, record_id)`; a record belongs
  to exactly one month (its `recorded_at`), so this is the same as `(stream, record_id)`.
- `audit.chain_checkpoints` is the checkpoint chain: one continuous, gap-free chain per stream,
  never partitioned and never deleted. A trigger accepts a checkpoint only as the next number
  of its stream, linked to the previous checkpoint's hash, so the chain cannot fork. At most
  one partition-final checkpoint and one retention tombstone exist per partition.
- Both tables are insert-only: UPDATE, DELETE and TRUNCATE are never granted, and the
  append-only triggers refuse them for every role, including the owner. Only the worker may
  insert links; the worker (daily and partition-final) and the retention role (tombstones)
  may insert checkpoints. Every runtime role reads them through the schema's default grant.
- `audit.ensure_partitions` also creates each audit partition's chain partitions, and fails
  loudly on a same-named table that is not attached where it should be.
- `audit.drop_expired_partition(stream, month)` removes a month's audit partition and its
  chain partition. Only the owner can detach partitions, so it is SECURITY DEFINER with a
  fixed search_path; only `hrms_audit_retention` may execute it, and it refuses unless the
  partition's retention tombstone is already in the checkpoint chain and the month can no
  longer receive rows. The tombstone's signature is the worker's; the database cannot check
  it, verification does.
- The outbox accepts the `audit.chain_mismatch` alert email.
"""

from collections.abc import Sequence

from alembic import op

from migrations.support import AUDIT_RETENTION_ROLE, WORKER_ROLE, quote_ident

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STREAMS = ("audit_log", "security_events")
MONTH_KEY = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
TEMPLATES = (
    "identity.invite",
    "identity.password_reset",
    "identity.password_changed",
    "identity.new_device",
    "identity.account_locked",
    "identity.recovery_code_used",
    "identity.mfa_changed",
    "identity.mfa_reset",
    "access.grant_request",
)

# The 0011 body with {chain_partitions} where this revision adds the chain partitions; the
# downgrade renders it without them, which is exactly the 0011 function.
FUNCTION = """
CREATE OR REPLACE FUNCTION audit.ensure_partitions(months_ahead integer) RETURNS integer
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
        -- A default partition would take rows for months without a partition, so a missing
        -- month would go unnoticed instead of failing the audited change.
        IF EXISTS (
            SELECT FROM pg_partitioned_table
            WHERE partrelid = to_regclass(format('audit.%I', parent)) AND partdefid <> 0
        ) THEN
            RAISE EXCEPTION 'audit.% has a default partition', parent;
        END IF;
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
                -- Attached, but possibly for another range: then this month has no partition.
                IF (
                    SELECT pg_get_expr(relpartbound, oid) FROM pg_class
                    WHERE oid = to_regclass(format('audit.%I', partition_name))
                ) IS DISTINCT FROM format(
                    'FOR VALUES FROM (%L) TO (%L)',
                    (month_start::timestamp AT TIME ZONE 'UTC')::text,
                    ((month_start + interval '1 month')::timestamp AT TIME ZONE 'UTC')::text
                ) THEN
                    RAISE EXCEPTION 'audit.% is attached for the wrong range', partition_name;
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
        END LOOP;{chain_partitions}
    END LOOP;
    RETURN created;
END
$$
"""

# For every month partition attached to the stream (the new ones above and any older one),
# the chain partition `chain_links_<stream>_pYYYY_MM`, attached to `chain_links_<stream>` for
# that month's key. The month comes from the partition's name, which only this function gives.
CHAIN_PARTITIONS = """
        FOR partition_name IN
            SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = to_regclass(format('audit.%I', parent))
              AND c.relname ~ '_p[0-9]{4}_[0-9]{2}$'
            ORDER BY c.relname
        LOOP
            DECLARE
                month_key text := regexp_replace(
                    substring(partition_name from '_p([0-9]{4}_[0-9]{2})$'), '_', '-'
                );
                chain_parent text := 'chain_links_' || parent;
                chain_name text := format('chain_links_%s_p%s', parent, replace(month_key, '-', '_'));
            BEGIN
                IF to_regclass(format('audit.%I', chain_name)) IS NOT NULL THEN
                    IF NOT EXISTS (
                        SELECT FROM pg_inherits
                        WHERE inhrelid = to_regclass(format('audit.%I', chain_name))
                          AND inhparent = to_regclass(format('audit.%I', chain_parent))
                    ) OR (
                        SELECT pg_get_expr(relpartbound, oid) FROM pg_class
                        WHERE oid = to_regclass(format('audit.%I', chain_name))
                    ) IS DISTINCT FROM format('FOR VALUES IN (%L)', month_key) THEN
                        RAISE EXCEPTION 'audit.% is not the chain partition of %', chain_name, month_key;
                    END IF;
                    CONTINUE;
                END IF;
                EXECUTE format(
                    'CREATE TABLE audit.%I (LIKE audit.chain_links INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
                    chain_name
                );
                EXECUTE format(
                    'ALTER TABLE audit.%I ATTACH PARTITION audit.%I FOR VALUES IN (%L)',
                    chain_parent, chain_name, month_key
                );
                EXECUTE format(
                    'CREATE TRIGGER %I BEFORE TRUNCATE ON audit.%I '
                    'FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_modification()',
                    chain_name || '_no_truncate', chain_name
                );
                created := created + 1;
            END;
        END LOOP;"""

DROP_EXPIRED_PARTITION = f"""
CREATE FUNCTION audit.drop_expired_partition(stream_name text, month_key text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET "TimeZone" = 'UTC'
SET lock_timeout = '5s'
AS $$
DECLARE
    suffix text;
    dropped boolean := false;
BEGIN
    IF stream_name IS NULL OR stream_name NOT IN ('audit_log', 'security_events') THEN
        RAISE EXCEPTION 'unknown audit stream';
    END IF;
    IF month_key IS NULL OR month_key !~ '{MONTH_KEY}' THEN
        RAISE EXCEPTION 'a month is written YYYY-MM';
    END IF;
    -- A month that can still receive rows is never removed (the partition-final margin).
    IF to_date(month_key || '-01', 'YYYY-MM-DD')::timestamp + interval '1 month 1 hour'
           > (now() AT TIME ZONE 'UTC') THEN
        RAISE EXCEPTION 'audit % for % can still receive rows', stream_name, month_key;
    END IF;
    -- Retention removes a partition only after its tombstone is in the checkpoint chain.
    IF NOT EXISTS (
        SELECT FROM audit.chain_checkpoints c
        WHERE c.stream = stream_name AND c.partition_key = month_key AND c.kind = 'retention_tombstone'
    ) THEN
        RAISE EXCEPTION 'audit % for % has no retention tombstone', stream_name, month_key;
    END IF;
    suffix := '_p' || replace(month_key, '-', '_');
    IF to_regclass(format('audit.%I', stream_name || suffix)) IS NOT NULL THEN
        EXECUTE format('ALTER TABLE audit.%I DETACH PARTITION audit.%I', stream_name, stream_name || suffix);
        EXECUTE format('DROP TABLE audit.%I', stream_name || suffix);
        dropped := true;
    END IF;
    IF to_regclass(format('audit.%I', 'chain_links_' || stream_name || suffix)) IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE audit.%I DETACH PARTITION audit.%I',
            'chain_links_' || stream_name, 'chain_links_' || stream_name || suffix
        );
        EXECUTE format('DROP TABLE audit.%I', 'chain_links_' || stream_name || suffix);
        dropped := true;
    END IF;
    RETURN dropped;
END
$$
"""


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _outbox_templates(templates: Sequence[str]) -> None:
    op.execute("ALTER TABLE notify.email_outbox DROP CONSTRAINT ck_email_outbox_template")
    op.execute(
        f"ALTER TABLE notify.email_outbox ADD CONSTRAINT ck_email_outbox_template "
        f"CHECK (template IN ({_in(templates)}))"
    )


def _no_truncate(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON audit.{table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_modification()"
    )


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE audit.chain_links (
            stream text NOT NULL,
            partition_key text NOT NULL,
            position bigint NOT NULL,
            record_id uuid NOT NULL,
            digest_version smallint NOT NULL,
            row_digest bytea NOT NULL,
            link_hash bytea NOT NULL,
            sealed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_chain_links PRIMARY KEY (stream, partition_key, position),
            CONSTRAINT uq_chain_links_stream_partition_key_record_id
                UNIQUE (stream, partition_key, record_id),
            CONSTRAINT ck_chain_links_stream CHECK (stream IN ({_in(STREAMS)})),
            CONSTRAINT ck_chain_links_partition_key CHECK (partition_key ~ '{MONTH_KEY}'),
            CONSTRAINT ck_chain_links_position CHECK (position >= 1),
            CONSTRAINT ck_chain_links_digest_version CHECK (digest_version >= 1),
            CONSTRAINT ck_chain_links_hashes CHECK (
                octet_length(row_digest) = 32 AND octet_length(link_hash) = 32
            )
        ) PARTITION BY LIST (stream)
        """
    )
    for stream in STREAMS:
        op.execute(
            f"CREATE TABLE audit.chain_links_{stream} PARTITION OF audit.chain_links "
            f"FOR VALUES IN ('{stream}') PARTITION BY LIST (partition_key)"
        )
    op.execute(
        f"""
        CREATE TABLE audit.chain_checkpoints (
            stream text NOT NULL,
            checkpoint_no bigint NOT NULL,
            kind text NOT NULL,
            partition_key text NOT NULL,
            last_position bigint NOT NULL,
            row_count bigint NOT NULL,
            last_link_hash bytea NOT NULL,
            prev_checkpoint_hash bytea NOT NULL,
            checkpoint_hash bytea NOT NULL,
            signature bytea NOT NULL,
            key_id text NOT NULL,
            anchor_object_key text NOT NULL,
            archive_sha256 bytea,
            retention_policy_version text,
            created_at timestamptz NOT NULL,
            CONSTRAINT pk_chain_checkpoints PRIMARY KEY (stream, checkpoint_no),
            CONSTRAINT ck_chain_checkpoints_stream CHECK (stream IN ({_in(STREAMS)})),
            CONSTRAINT ck_chain_checkpoints_kind CHECK (
                kind IN ('daily', 'partition_final', 'retention_tombstone')
            ),
            CONSTRAINT ck_chain_checkpoints_partition_key CHECK (partition_key ~ '{MONTH_KEY}'),
            CONSTRAINT ck_chain_checkpoints_numbers CHECK (
                checkpoint_no >= 1 AND last_position >= 0 AND row_count = last_position
            ),
            CONSTRAINT ck_chain_checkpoints_hashes CHECK (
                octet_length(last_link_hash) = 32 AND octet_length(prev_checkpoint_hash) = 32
                AND octet_length(checkpoint_hash) = 32 AND octet_length(signature) = 64
            ),
            CONSTRAINT ck_chain_checkpoints_key_id CHECK (key_id ~ '^[A-Za-z0-9._-]{{1,64}}$'),
            CONSTRAINT ck_chain_checkpoints_anchor_object_key CHECK (
                char_length(anchor_object_key) BETWEEN 1 AND 200
            ),
            CONSTRAINT ck_chain_checkpoints_tombstone CHECK (
                (kind = 'retention_tombstone') = (retention_policy_version IS NOT NULL)
                AND (archive_sha256 IS NULL
                    OR (kind = 'retention_tombstone' AND octet_length(archive_sha256) = 32))
                AND (retention_policy_version IS NULL
                    OR char_length(retention_policy_version) BETWEEN 1 AND 100)
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_chain_checkpoints_partition_final ON audit.chain_checkpoints "
        "(stream, partition_key) WHERE kind = 'partition_final'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_chain_checkpoints_retention_tombstone ON audit.chain_checkpoints "
        "(stream, partition_key) WHERE kind = 'retention_tombstone'"
    )
    op.execute(
        """
        CREATE FUNCTION audit.check_checkpoint_sequence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            previous_no bigint;
            previous_hash bytea;
        BEGIN
            SELECT checkpoint_no, checkpoint_hash INTO previous_no, previous_hash
            FROM audit.chain_checkpoints WHERE stream = NEW.stream
            ORDER BY checkpoint_no DESC LIMIT 1;
            IF previous_no IS NULL THEN
                previous_no := 0;
                previous_hash := '\\x' || repeat('00', 32);
            END IF;
            IF NEW.checkpoint_no <> previous_no + 1 OR NEW.prev_checkpoint_hash <> previous_hash THEN
                RAISE EXCEPTION 'a checkpoint must follow checkpoint % of %', previous_no, NEW.stream
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER chain_checkpoints_sequence BEFORE INSERT ON audit.chain_checkpoints "
        "FOR EACH ROW EXECUTE FUNCTION audit.check_checkpoint_sequence()"
    )
    for table in ("chain_links", "chain_checkpoints"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON audit.{table} "
            "FOR EACH ROW EXECUTE FUNCTION audit.reject_modification()"
        )
        _no_truncate(table)
    for stream in STREAMS:
        _no_truncate(f"chain_links_{stream}")

    op.execute(FUNCTION.replace("{chain_partitions}", CHAIN_PARTITIONS))
    op.execute("SELECT audit.ensure_partitions(3)")
    op.execute(DROP_EXPIRED_PARTITION)

    worker, retention = quote_ident(WORKER_ROLE), quote_ident(AUDIT_RETENTION_ROLE)
    op.execute(f"GRANT INSERT ON audit.chain_links TO {worker}")
    op.execute(f"GRANT INSERT ON audit.chain_checkpoints TO {worker}, {retention}")
    op.execute(f"GRANT EXECUTE ON FUNCTION audit.drop_expired_partition(text, text) TO {retention}")
    _outbox_templates((*TEMPLATES, "audit.chain_mismatch"))


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    _outbox_templates(TEMPLATES)
    op.execute("DROP FUNCTION audit.drop_expired_partition(text, text)")
    op.execute(FUNCTION.replace("{chain_partitions}", ""))
    op.execute("DROP TABLE audit.chain_checkpoints")
    op.execute("DROP FUNCTION audit.check_checkpoint_sequence()")
    op.execute("DROP TABLE audit.chain_links")
