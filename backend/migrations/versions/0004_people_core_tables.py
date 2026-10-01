"""People core: employees and effective-dated job history (docs/database-design.md §4.4).

Revision ID: 0004
Revises: 0003

Expand/contract: new tables, and a foreign key on an org table that is still empty (no
write path exists before M2), so adding it validates instantly.

M1 needs only what links a user to an employee and resolves reporting lines. The personal,
sensitive and status-history tables arrive with M2, as do the directory-search indexes
(they come with the query that uses them; the tables are still empty then).

Job history is preserved (CLAUDE.md rule 5): a change closes the current row by setting
`effective_to` and inserts a new row. The guard trigger rejects any other update, every
delete and truncation, for every role including the owner.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE people.employees (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_code citext NOT NULL,
            legal_first_name text NOT NULL,
            legal_last_name text,
            preferred_name text,
            work_email citext,
            work_phone text,
            date_of_joining date NOT NULL,
            date_of_exit date,
            status text NOT NULL,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_employees PRIMARY KEY (id),
            CONSTRAINT uq_employees_employee_code UNIQUE (employee_code),
            CONSTRAINT uq_employees_work_email UNIQUE (work_email),
            CONSTRAINT ck_employees_employee_code CHECK (char_length(employee_code) BETWEEN 1 AND 32),
            CONSTRAINT ck_employees_legal_first_name CHECK (char_length(legal_first_name) BETWEEN 1 AND 100),
            CONSTRAINT ck_employees_legal_last_name CHECK (char_length(legal_last_name) BETWEEN 1 AND 100),
            CONSTRAINT ck_employees_preferred_name CHECK (char_length(preferred_name) BETWEEN 1 AND 100),
            CONSTRAINT ck_employees_work_email CHECK (
                char_length(work_email) <= 254 AND work_email ~ '^[^@[:space:]]+@[^@[:space:]]+$'
            ),
            CONSTRAINT ck_employees_work_phone CHECK (char_length(work_phone) BETWEEN 1 AND 32),
            CONSTRAINT ck_employees_status CHECK (status IN ('pre_joining', 'active', 'on_notice', 'exited')),
            CONSTRAINT ck_employees_exit_after_joining CHECK (
                date_of_exit IS NULL OR date_of_exit >= date_of_joining
            ),
            CONSTRAINT ck_employees_exited_has_exit_date CHECK (
                status <> 'exited' OR date_of_exit IS NOT NULL
            ),
            CONSTRAINT ck_employees_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE people.employee_jobs (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            effective_from date NOT NULL,
            effective_to date,
            department_id uuid NOT NULL,
            designation_id uuid NOT NULL,
            location_id uuid NOT NULL,
            manager_employee_id uuid,
            employment_type text NOT NULL,
            change_reason text NOT NULL,
            notes text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_employee_jobs PRIMARY KEY (id),
            CONSTRAINT fk_employee_jobs_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_employee_jobs_department_id FOREIGN KEY (department_id)
                REFERENCES org.departments (id) ON DELETE RESTRICT,
            CONSTRAINT fk_employee_jobs_designation_id FOREIGN KEY (designation_id)
                REFERENCES org.designations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_employee_jobs_location_id FOREIGN KEY (location_id)
                REFERENCES org.locations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_employee_jobs_manager_employee_id FOREIGN KEY (manager_employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT ck_employee_jobs_effective_range CHECK (
                effective_to IS NULL OR effective_to > effective_from
            ),
            CONSTRAINT ck_employee_jobs_manager_not_self CHECK (manager_employee_id <> employee_id),
            CONSTRAINT ck_employee_jobs_employment_type CHECK (
                employment_type IN ('full_time', 'part_time', 'contract', 'intern')
            ),
            CONSTRAINT ck_employee_jobs_change_reason CHECK (
                change_reason IN ('joining', 'transfer', 'promotion', 'manager_change', 'correction', 'other')
            ),
            CONSTRAINT ck_employee_jobs_notes CHECK (char_length(notes) <= 1000),
            CONSTRAINT ex_employee_jobs_employee_id_effective EXCLUDE USING gist (
                employee_id WITH =, daterange(effective_from, effective_to) WITH &&
            )
        )
        """
    )
    # Team resolution walks reporting lines downwards (manager -> reports); the exclusion
    # constraint's GiST index already serves lookups by employee_id. Department and location
    # indexes serve role assignments restricted to a department or location.
    op.execute(
        "CREATE INDEX ix_employee_jobs_manager_employee_id ON people.employee_jobs (manager_employee_id) "
        "WHERE manager_employee_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_employee_jobs_department_id ON people.employee_jobs (department_id)")
    op.execute("CREATE INDEX ix_employee_jobs_location_id ON people.employee_jobs (location_id)")

    op.execute(
        """
        CREATE FUNCTION people.guard_job_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF (to_jsonb(NEW) - 'effective_to' - 'updated_at')
                   IS DISTINCT FROM (to_jsonb(OLD) - 'effective_to' - 'updated_at') THEN
                    RAISE EXCEPTION 'job history is preserved: only effective_to can change'
                        USING HINT = 'Close the current row and insert a new one.';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'job history is preserved: % is not allowed on %', TG_OP, TG_TABLE_NAME;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_job_history_rows BEFORE UPDATE OR DELETE ON people.employee_jobs "
        "FOR EACH ROW EXECUTE FUNCTION people.guard_job_history()"
    )
    op.execute(
        "CREATE TRIGGER guard_job_history_truncate BEFORE TRUNCATE ON people.employee_jobs "
        "FOR EACH STATEMENT EXECUTE FUNCTION people.guard_job_history()"
    )

    op.execute(
        "ALTER TABLE org.departments ADD CONSTRAINT fk_departments_head_employee_id "
        "FOREIGN KEY (head_employee_id) REFERENCES people.employees (id) ON DELETE RESTRICT"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("ALTER TABLE org.departments DROP CONSTRAINT fk_departments_head_employee_id")
    op.execute("DROP TABLE people.employee_jobs")
    op.execute("DROP FUNCTION people.guard_job_history()")
    op.execute("DROP TABLE people.employees")
