"""Attendance events: the append-only source of truth for clocking (docs/database-design.md §4.5).

Revision ID: 0015
Revises: 0014

Expand only: a new `attendance` schema with the module-schema grants of revision 0001, and
`attendance.attendance_events`.

Events are never updated or deleted (CLAUDE.md rule 5): a trigger refuses UPDATE, DELETE and
TRUNCATE for every role, including the owner. Corrections arrive later as new events plus
void rows, so the original events always remain.

This revision records events only. Shifts, attendance policies, the day projection and
corrections (the rest of §4.5) need Sigvitas's shift and policy values (roadmap §6) and arrive
with them; until then no day is judged present, late or absent. `correction_id` and `geo` are
added with the features that write them.
"""

from collections.abc import Sequence

from alembic import op

from migrations.support import RUNTIME_ROLES, quote_ident

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "attendance"


def _roles() -> str:
    return ", ".join(quote_ident(role) for role in RUNTIME_ROLES)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {SCHEMA}")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {_roles()}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_roles()}"
    )
    op.execute(
        """
        CREATE TABLE attendance.attendance_events (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            event_type text NOT NULL,
            occurred_at timestamptz NOT NULL,
            work_date date NOT NULL,
            source text NOT NULL,
            recorded_by uuid NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            client_reported_at timestamptz,
            ip inet,
            user_agent text,
            idempotency_key text NOT NULL,
            CONSTRAINT pk_attendance_events PRIMARY KEY (id),
            CONSTRAINT fk_attendance_events_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_attendance_events_recorded_by FOREIGN KEY (recorded_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_attendance_events_employee_id_idempotency_key UNIQUE (employee_id, idempotency_key),
            CONSTRAINT ck_attendance_events_event_type CHECK (
                event_type IN ('CLOCK_IN', 'BREAK_START', 'BREAK_END', 'CLOCK_OUT')
            ),
            CONSTRAINT ck_attendance_events_source CHECK (
                source IN ('web', 'mobile', 'correction', 'import')
            ),
            CONSTRAINT ck_attendance_events_user_agent CHECK (char_length(user_agent) <= 512),
            CONSTRAINT ck_attendance_events_idempotency_key CHECK (
                char_length(idempotency_key) BETWEEN 8 AND 100
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_attendance_events_employee_id_work_date_occurred_at "
        "ON attendance.attendance_events (employee_id, work_date, occurred_at)"
    )
    op.execute("CREATE INDEX ix_attendance_events_work_date ON attendance.attendance_events (work_date)")
    op.execute(
        """
        CREATE FUNCTION attendance.guard_events() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'attendance events are append-only: % is not allowed on %', TG_OP, TG_TABLE_NAME
                USING HINT = 'Record a correction instead.';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_events_rows BEFORE UPDATE OR DELETE ON attendance.attendance_events "
        "FOR EACH ROW EXECUTE FUNCTION attendance.guard_events()"
    )
    op.execute(
        "CREATE TRIGGER guard_events_truncate BEFORE TRUNCATE ON attendance.attendance_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION attendance.guard_events()"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE attendance.attendance_events")
    op.execute("DROP FUNCTION attendance.guard_events()")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_roles()}"
    )
    op.execute(f"DROP SCHEMA {SCHEMA}")
