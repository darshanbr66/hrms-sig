"""Organization and people records for M2 (docs/database-design.md §4.3, §4.4).

Revision ID: 0014
Revises: 0013

Expand only:

- `version` on the org tables, for optimistic concurrency on their PATCH routes
  (docs/api-architecture.md §3). The column has a constant default, so adding it does not
  rewrite the tables.
- `people.employee_personal` (the `personal` tier, 1:1 with employees) and
  `people.employee_status_history` (append-only, guarded like job history).
- The directory-search indexes deferred from revision 0004: status, and a trigram index on
  the name fields, the employee code and the work email (`pg_trgm` is installed by revision 0001). The
  tables hold no rows before M2, so the indexes build instantly.
- Data: `org.read` and `holiday.read` for `hr` and `hr_admin`. Both roles create employees
  and maintain the organization, which needs the departments, designations, locations and
  holiday calendars to choose from, and an HR account is not always linked to an employee
  record (which is what grants them to `employee`). Neither permission reaches personal data.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG_TABLES = ("locations", "departments", "designations")
# The directory search expression; the query uses exactly this text so the index applies.
SEARCH_TEXT = (
    "legal_first_name || ' ' || coalesce(legal_last_name, '') || ' ' || coalesce(preferred_name, '') "
    "|| ' ' || employee_code::text || ' ' || coalesce(work_email::text, '')"
)
ROLE_GRANTS = (
    ("hr", "org.read"),
    ("hr", "holiday.read"),
    ("hr_admin", "org.read"),
    ("hr_admin", "holiday.read"),
)


def upgrade() -> None:
    for table in ORG_TABLES:
        op.execute(f"ALTER TABLE org.{table} ADD COLUMN version integer NOT NULL DEFAULT 1")
        op.execute(f"ALTER TABLE org.{table} ADD CONSTRAINT ck_{table}_version CHECK (version >= 1)")

    op.execute(
        """
        CREATE TABLE people.employee_personal (
            employee_id uuid NOT NULL,
            date_of_birth date,
            personal_email citext,
            personal_phone text,
            current_address jsonb,
            permanent_address jsonb,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_employee_personal PRIMARY KEY (employee_id),
            CONSTRAINT fk_employee_personal_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT ck_employee_personal_personal_email CHECK (
                char_length(personal_email) <= 254 AND personal_email ~ '^[^@[:space:]]+@[^@[:space:]]+$'
            ),
            CONSTRAINT ck_employee_personal_personal_phone CHECK (
                char_length(personal_phone) BETWEEN 1 AND 32
            ),
            CONSTRAINT ck_employee_personal_current_address CHECK (
                jsonb_typeof(current_address) = 'object' AND octet_length(current_address::text) <= 2048
            ),
            CONSTRAINT ck_employee_personal_permanent_address CHECK (
                jsonb_typeof(permanent_address) = 'object' AND octet_length(permanent_address::text) <= 2048
            ),
            CONSTRAINT ck_employee_personal_version CHECK (version >= 1)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE people.employee_status_history (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            from_status text NOT NULL,
            to_status text NOT NULL,
            effective_date date NOT NULL,
            reason text NOT NULL,
            changed_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_employee_status_history PRIMARY KEY (id),
            CONSTRAINT fk_employee_status_history_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_employee_status_history_changed_by FOREIGN KEY (changed_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_employee_status_history_from_status CHECK (
                from_status IN ('pre_joining', 'active', 'on_notice', 'exited')
            ),
            CONSTRAINT ck_employee_status_history_to_status CHECK (
                to_status IN ('pre_joining', 'active', 'on_notice', 'exited')
            ),
            CONSTRAINT ck_employee_status_history_changed CHECK (from_status <> to_status),
            CONSTRAINT ck_employee_status_history_reason CHECK (char_length(reason) BETWEEN 1 AND 500)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_employee_status_history_employee_id ON people.employee_status_history (employee_id)"
    )
    op.execute(
        """
        CREATE FUNCTION people.guard_status_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'status history is preserved: % is not allowed on %', TG_OP, TG_TABLE_NAME;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_status_history_rows BEFORE UPDATE OR DELETE ON people.employee_status_history "
        "FOR EACH ROW EXECUTE FUNCTION people.guard_status_history()"
    )
    op.execute(
        "CREATE TRIGGER guard_status_history_truncate BEFORE TRUNCATE ON people.employee_status_history "
        "FOR EACH STATEMENT EXECUTE FUNCTION people.guard_status_history()"
    )

    op.execute("CREATE INDEX ix_employees_status ON people.employees (status)")
    op.execute(
        f"CREATE INDEX ix_employees_search ON people.employees USING gin (({SEARCH_TEXT}) gin_trgm_ops)"
    )

    for role, permission in ROLE_GRANTS:
        op.execute(
            "INSERT INTO access.role_permissions (role_id, permission_id) "
            f"SELECT r.id, p.id FROM access.roles r, access.permissions p "
            f"WHERE r.key = '{role}' AND p.key = '{permission}'"
        )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    for role, permission in ROLE_GRANTS:
        op.execute(
            "DELETE FROM access.role_permissions WHERE "
            f"role_id = (SELECT id FROM access.roles WHERE key = '{role}') AND "
            f"permission_id = (SELECT id FROM access.permissions WHERE key = '{permission}')"
        )
    op.execute("DROP INDEX people.ix_employees_search")
    op.execute("DROP INDEX people.ix_employees_status")
    op.execute("DROP TABLE people.employee_status_history")
    op.execute("DROP FUNCTION people.guard_status_history()")
    op.execute("DROP TABLE people.employee_personal")
    for table in reversed(ORG_TABLES):
        op.execute(f"ALTER TABLE org.{table} DROP COLUMN version")
