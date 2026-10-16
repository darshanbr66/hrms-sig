"""Export jobs (docs/database-design.md §4.11, product-requirements RPT-1 to RPT-3).

Revision ID: 0023
Revises: 0022

Expand only, in the `app` schema of revision 0001.

`app.export_jobs`: one row per requested export. The request fixes, at the moment it is
made (after step-up), what the export may contain: the type, the period and the employees
the requester could read then. The worker writes the CSV file to private storage and marks
the row `done`; the file can be downloaded by the requester only, until `expires_at` (24 hours
after it was produced), after which the worker removes it and marks the row `expired`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPORT_TYPES = (
    "attendance_summary",
    "attendance_days",
    "leave_requests",
    "leave_balances",
    "employees",
    "audit_log",
)


def upgrade() -> None:
    types = ", ".join(f"'{value}'" for value in EXPORT_TYPES)
    op.execute(
        f"""
        CREATE TABLE app.export_jobs (
            id uuid NOT NULL DEFAULT uuidv7(),
            requested_by uuid NOT NULL,
            export_type text NOT NULL,
            parameters jsonb NOT NULL,
            status text NOT NULL DEFAULT 'queued',
            attempts smallint NOT NULL DEFAULT 0,
            row_count integer,
            storage_key text,
            file_name text,
            error_code text,
            requested_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            completed_at timestamptz,
            expires_at timestamptz,
            downloaded_at timestamptz,
            CONSTRAINT pk_export_jobs PRIMARY KEY (id),
            CONSTRAINT fk_export_jobs_requested_by FOREIGN KEY (requested_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_export_jobs_storage_key UNIQUE (storage_key),
            CONSTRAINT ck_export_jobs_export_type CHECK (export_type IN ({types})),
            CONSTRAINT ck_export_jobs_status CHECK (
                status IN ('queued', 'running', 'done', 'failed', 'expired')
            ),
            CONSTRAINT ck_export_jobs_parameters CHECK (
                jsonb_typeof(parameters) = 'object' AND octet_length(parameters::text) <= 262144
            ),
            CONSTRAINT ck_export_jobs_storage_key CHECK (storage_key ~ '^exports/[0-9a-f-]{{36}}$'),
            CONSTRAINT ck_export_jobs_file_name CHECK (char_length(file_name) BETWEEN 1 AND 120),
            CONSTRAINT ck_export_jobs_error_code CHECK (char_length(error_code) BETWEEN 1 AND 100),
            CONSTRAINT ck_export_jobs_done CHECK (
                status <> 'done'
                OR (storage_key IS NOT NULL AND completed_at IS NOT NULL AND expires_at IS NOT NULL)
            ),
            CONSTRAINT ck_export_jobs_attempts CHECK (attempts BETWEEN 0 AND 100)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_export_jobs_due ON app.export_jobs (requested_at) "
        "WHERE status IN ('queued', 'running', 'done')"
    )
    op.execute(
        "CREATE INDEX ix_export_jobs_requested_by_requested_at "
        "ON app.export_jobs (requested_by, requested_at)"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE app.export_jobs")
