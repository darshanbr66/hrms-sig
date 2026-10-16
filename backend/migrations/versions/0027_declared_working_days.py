"""Declared working days in holiday calendars (docs/database-design.md §4.6, product-requirements LV-7).

Revision ID: 0027
Revises: 0026

Expand only. A holiday calendar entry is now either a `holiday` (as before; every existing row
gets this kind) or a `working_day`: a date the people at the calendar's locations work even
though their shift has it as a weekly off, for example a Saturday management has announced as
a working day. The entry overrides the weekly off for that one date only; the shift and its
weekly offs are not changed. A working day is never optional, and one calendar holds at most
one entry per date (the existing unique constraint), so a date cannot be both.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE leave.holidays ADD COLUMN kind text NOT NULL DEFAULT 'holiday'")
    op.execute(
        "ALTER TABLE leave.holidays ADD CONSTRAINT ck_holidays_kind "
        "CHECK (kind IN ('holiday', 'working_day'))"
    )
    op.execute(
        "ALTER TABLE leave.holidays ADD CONSTRAINT ck_holidays_working_day_not_optional "
        "CHECK (kind = 'holiday' OR NOT is_optional)"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("ALTER TABLE leave.holidays DROP CONSTRAINT ck_holidays_working_day_not_optional")
    op.execute("ALTER TABLE leave.holidays DROP CONSTRAINT ck_holidays_kind")
    op.execute("ALTER TABLE leave.holidays DROP COLUMN kind")
