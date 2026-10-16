"""Repair the export storage-key check (docs/database-design.md §4.11).

Revision ID: 0026
Revises: 0025

An early draft of revision 0023 built `ck_export_jobs_storage_key` from an f-string, so a
database migrated with that draft holds `'^exports/[0-9a-f-]36$'` instead of
`'^exports/[0-9a-f-]{36}$'`, a pattern no storage key can match: every finished export would be
refused. This revision puts the intended check in place on every database. Where 0023 was
already correct it replaces the check with the same one. No existing row can break it, because
the faulty check never let a storage key be written.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.export_jobs DROP CONSTRAINT ck_export_jobs_storage_key")
    op.execute(
        "ALTER TABLE app.export_jobs ADD CONSTRAINT ck_export_jobs_storage_key "
        "CHECK (storage_key ~ '^exports/[0-9a-f-]{36}$')"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production.

    Nothing to undo: the check stays the one revision 0023 intends.
    """
