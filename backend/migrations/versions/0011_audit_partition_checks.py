"""Audit partition maintenance checks the partitions it finds (docs/database-design.md §4.10).

Revision ID: 0011
Revises: 0010

Expand/contract: replaces one function body; no table changes. Grants, owner and
SECURITY DEFINER are kept (CREATE OR REPLACE keeps the function's privileges).

`audit.ensure_partitions` (revision 0005) treated a month as covered when a partition with
the month's name was attached to the stream, whatever range it was attached for, and it
did not look for a default partition. Either would leave a month without its own partition
while the hourly job reported success, and a default partition would silently take rows
that must instead fail the audited change. The function now fails loudly when a stream has
a default partition, or when a month's partition is attached for another range.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The body shared by both versions, with {checks} where they differ. Runs with TimeZone UTC,
# so a timestamptz bound renders as 'YYYY-MM-01 00:00:00+00' in pg_get_expr and in ::text.
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
{default_check}
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
{range_check}
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

DEFAULT_CHECK = """\
        -- A default partition would take rows for months without a partition, so a missing
        -- month would go unnoticed instead of failing the audited change.
        IF EXISTS (
            SELECT FROM pg_partitioned_table
            WHERE partrelid = to_regclass(format('audit.%I', parent)) AND partdefid <> 0
        ) THEN
            RAISE EXCEPTION 'audit.% has a default partition', parent;
        END IF;"""

RANGE_CHECK = """\
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
                END IF;"""


def upgrade() -> None:
    op.execute(FUNCTION.format(default_check=DEFAULT_CHECK, range_check=RANGE_CHECK))


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute(FUNCTION.format(default_check="", range_check=""))
