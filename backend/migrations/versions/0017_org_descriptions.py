"""Department descriptions and the designation index for M2 organization management
(docs/database-design.md §4.3, §4.4).

Revision ID: 0017
Revises: 0016

Expand only:

- `org.departments.description`: optional free text (1 to 1000 characters) saying what the
  department does. Nullable with no default, so adding it does not rewrite the table.
- `ix_employee_jobs_designation_id`: the employee list filters by designation, and archiving a
  designation checks for employees still placed in it. Department and location already have
  their indexes (revision 0004).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE org.departments ADD COLUMN description text")
    op.execute(
        "ALTER TABLE org.departments ADD CONSTRAINT ck_departments_description "
        "CHECK (char_length(description) BETWEEN 1 AND 1000)"
    )
    op.execute("CREATE INDEX ix_employee_jobs_designation_id ON people.employee_jobs (designation_id)")


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP INDEX people.ix_employee_jobs_designation_id")
    op.execute("ALTER TABLE org.departments DROP COLUMN description")
