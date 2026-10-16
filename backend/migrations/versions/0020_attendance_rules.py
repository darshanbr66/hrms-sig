"""Shifts, attendance policies, corrections, voids and locks (docs/database-design.md §4.5,
product-requirements ATT-4 to ATT-12).

Revision ID: 0020
Revises: 0019

Expand only, in the `attendance` schema of revision 0015 (whose default privileges grant the
runtime roles). Nothing is seeded: every shift time, weekly off, threshold and grace period is
a value Sigvitas HR enters (product-requirements §6.12), and every rule column is NOT NULL
without a default, so the schema assumes none.

- `shifts` and `shift_versions`: when work is expected, versioned by effective date. A version
  may cross midnight (`crosses_midnight` must agree with the times); its weekly offs are a
  validated JSON list of weekdays, each optionally limited to some weeks of the month.
- `location_default_shifts` and `shift_assignments`: which shift applies to an employee on a
  date (their own assignment first, else their location's default). Ranges of one location or
  employee cannot overlap.
- `attendance_policies`: how a day is judged, effective-dated per scope (organization,
  location or shift; the most specific applies). Ranges of one scope cannot overlap.
- Versioned rows are never edited: only an open `effective_to` may be set, when a newer row
  takes over (trigger `attendance.guard_versioned`).
- `attendance_corrections` and `attendance_correction_items`: an employee's proposed changes
  to one work date (events to add, events to void), or HR's recorded changes. One pending
  correction per employee and date. A correction moves once from `pending` to its outcome
  (trigger); items are append-only.
- `attendance_event_voids`: an approved correction voids an event by adding a row here; the
  event itself stays (CLAUDE.md rule 5). Append-only, one void per event.
- `attendance_events.correction_id`: events added by an approved correction.
- `attendance_locks`: closed date ranges. Corrections inside need the `all` scope and are
  flagged in the audit log. A lock is released by recording who released it and why, never
  deleted; active locks cannot overlap.
- The `attendance.correction` email template on the outbox.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES_BEFORE = (
    "identity.invite",
    "identity.password_reset",
    "identity.password_changed",
    "identity.new_device",
    "identity.account_locked",
    "identity.recovery_code_used",
    "identity.mfa_changed",
    "identity.mfa_reset",
    "access.grant_request",
    "audit.chain_mismatch",
    "leave.request",
)
NO_SCOPE = "'00000000-0000-0000-0000-000000000000'::uuid"


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _outbox_templates(templates: Sequence[str]) -> None:
    op.execute("ALTER TABLE notify.email_outbox DROP CONSTRAINT ck_email_outbox_template")
    op.execute(
        f"ALTER TABLE notify.email_outbox ADD CONSTRAINT ck_email_outbox_template "
        f"CHECK (template IN ({_in(templates)}))"
    )


def _guard(table: str, function: str) -> None:
    op.execute(
        f"CREATE TRIGGER {function}_rows BEFORE UPDATE OR DELETE ON attendance.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION attendance.{function}()"
    )
    op.execute(
        f"CREATE TRIGGER {function}_truncate BEFORE TRUNCATE ON attendance.{table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION attendance.{function}()"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION attendance.guard_versioned() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.effective_to IS NOT NULL
                   OR (to_jsonb(NEW) - 'effective_to') IS DISTINCT FROM (to_jsonb(OLD) - 'effective_to') THEN
                    RAISE EXCEPTION '% is versioned: only an open effective_to can be set', TG_TABLE_NAME
                        USING HINT = 'Create a new version instead.';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION '% is versioned: % is not allowed', TG_TABLE_NAME, TG_OP;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION attendance.guard_append_only() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING HINT = 'Record a new row instead.';
        END
        $$
        """
    )

    op.execute(
        """
        CREATE TABLE attendance.shifts (
            id uuid NOT NULL DEFAULT uuidv7(),
            code citext NOT NULL,
            name text NOT NULL,
            description text,
            status text NOT NULL DEFAULT 'active',
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_shifts PRIMARY KEY (id),
            CONSTRAINT uq_shifts_code UNIQUE (code),
            CONSTRAINT ck_shifts_code CHECK (char_length(code) BETWEEN 1 AND 32 AND code !~ '\\s'),
            CONSTRAINT ck_shifts_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_shifts_description CHECK (char_length(description) BETWEEN 1 AND 1000),
            CONSTRAINT ck_shifts_status CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_shifts_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE attendance.shift_versions (
            id uuid NOT NULL DEFAULT uuidv7(),
            shift_id uuid NOT NULL,
            version_no integer NOT NULL,
            effective_from date NOT NULL,
            effective_to date,
            start_time time NOT NULL,
            end_time time NOT NULL,
            crosses_midnight boolean NOT NULL,
            weekly_offs jsonb NOT NULL,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_shift_versions PRIMARY KEY (id),
            CONSTRAINT fk_shift_versions_shift_id FOREIGN KEY (shift_id)
                REFERENCES attendance.shifts (id) ON DELETE RESTRICT,
            CONSTRAINT fk_shift_versions_created_by FOREIGN KEY (created_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_shift_versions_shift_id_version_no UNIQUE (shift_id, version_no),
            CONSTRAINT ck_shift_versions_version_no CHECK (version_no >= 1),
            CONSTRAINT ck_shift_versions_range CHECK (effective_to IS NULL OR effective_to > effective_from),
            CONSTRAINT ck_shift_versions_times CHECK (
                start_time <> end_time AND crosses_midnight = (end_time < start_time)
            ),
            CONSTRAINT ck_shift_versions_weekly_offs CHECK (
                jsonb_typeof(weekly_offs) = 'array' AND jsonb_array_length(weekly_offs) <= 7
            ),
            CONSTRAINT ex_shift_versions_shift_id_effective EXCLUDE USING gist (
                shift_id WITH =, daterange(effective_from, effective_to) WITH &&
            )
        )
        """
    )
    _guard("shift_versions", "guard_versioned")

    op.execute(
        """
        CREATE TABLE attendance.location_default_shifts (
            id uuid NOT NULL DEFAULT uuidv7(),
            location_id uuid NOT NULL,
            shift_id uuid NOT NULL,
            effective_from date NOT NULL,
            effective_to date,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_location_default_shifts PRIMARY KEY (id),
            CONSTRAINT fk_location_default_shifts_location_id FOREIGN KEY (location_id)
                REFERENCES org.locations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_location_default_shifts_shift_id FOREIGN KEY (shift_id)
                REFERENCES attendance.shifts (id) ON DELETE RESTRICT,
            CONSTRAINT fk_location_default_shifts_created_by FOREIGN KEY (created_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_location_default_shifts_range CHECK (
                effective_to IS NULL OR effective_to > effective_from
            ),
            CONSTRAINT ex_location_default_shifts_location_id_effective EXCLUDE USING gist (
                location_id WITH =, daterange(effective_from, effective_to) WITH &&
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_location_default_shifts_shift_id ON attendance.location_default_shifts (shift_id)"
    )
    _guard("location_default_shifts", "guard_versioned")

    op.execute(
        """
        CREATE TABLE attendance.shift_assignments (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            shift_id uuid NOT NULL,
            effective_from date NOT NULL,
            effective_to date,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_shift_assignments PRIMARY KEY (id),
            CONSTRAINT fk_shift_assignments_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_shift_assignments_shift_id FOREIGN KEY (shift_id)
                REFERENCES attendance.shifts (id) ON DELETE RESTRICT,
            CONSTRAINT fk_shift_assignments_created_by FOREIGN KEY (created_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_shift_assignments_range CHECK (
                effective_to IS NULL OR effective_to > effective_from
            ),
            CONSTRAINT ex_shift_assignments_employee_id_effective EXCLUDE USING gist (
                employee_id WITH =, daterange(effective_from, effective_to) WITH &&
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_shift_assignments_shift_id ON attendance.shift_assignments (shift_id)")
    _guard("shift_assignments", "guard_versioned")

    op.execute(
        f"""
        CREATE TABLE attendance.attendance_policies (
            id uuid NOT NULL DEFAULT uuidv7(),
            scope_type text NOT NULL,
            scope_id uuid,
            effective_from date NOT NULL,
            effective_to date,
            grace_minutes integer NOT NULL,
            early_departure_grace_minutes integer NOT NULL,
            duration_basis text NOT NULL,
            full_day_min_minutes integer NOT NULL,
            half_day_min_minutes integer NOT NULL,
            max_break_minutes integer,
            breaks_count_as_work boolean NOT NULL,
            break_excess_action text NOT NULL,
            overtime_enabled boolean NOT NULL,
            overtime_basis text,
            overtime_min_minutes integer,
            overtime_requires_approval boolean NOT NULL,
            incomplete_after_minutes integer NOT NULL,
            allow_multiple_sessions boolean NOT NULL,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_attendance_policies PRIMARY KEY (id),
            CONSTRAINT fk_attendance_policies_created_by FOREIGN KEY (created_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_attendance_policies_scope CHECK (
                (scope_type = 'organization' AND scope_id IS NULL)
                OR (scope_type IN ('location', 'shift') AND scope_id IS NOT NULL)
            ),
            CONSTRAINT ck_attendance_policies_range CHECK (
                effective_to IS NULL OR effective_to > effective_from
            ),
            CONSTRAINT ck_attendance_policies_minutes CHECK (
                grace_minutes BETWEEN 0 AND 1440
                AND early_departure_grace_minutes BETWEEN 0 AND 1440
                AND full_day_min_minutes BETWEEN 1 AND 1440
                AND half_day_min_minutes BETWEEN 1 AND 1440
                AND half_day_min_minutes <= full_day_min_minutes
                AND incomplete_after_minutes BETWEEN 0 AND 1440
                AND (max_break_minutes IS NULL OR max_break_minutes BETWEEN 0 AND 1440)
            ),
            CONSTRAINT ck_attendance_policies_duration_basis CHECK (duration_basis IN ('effective', 'gross')),
            CONSTRAINT ck_attendance_policies_break_excess_action CHECK (
                break_excess_action IN ('flag', 'deduct')
            ),
            CONSTRAINT ck_attendance_policies_overtime CHECK (
                (overtime_enabled AND overtime_basis IN ('beyond_full_day', 'beyond_shift_end')
                    AND overtime_min_minutes BETWEEN 0 AND 1440)
                OR (NOT overtime_enabled AND overtime_basis IS NULL AND overtime_min_minutes IS NULL
                    AND NOT overtime_requires_approval)
            ),
            CONSTRAINT ex_attendance_policies_scope_effective EXCLUDE USING gist (
                scope_type WITH =,
                (coalesce(scope_id, {NO_SCOPE})) WITH =,
                daterange(effective_from, effective_to) WITH &&
            )
        )
        """
    )
    _guard("attendance_policies", "guard_versioned")

    op.execute(
        """
        CREATE TABLE attendance.attendance_corrections (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            work_date date NOT NULL,
            kind text NOT NULL,
            reason text NOT NULL,
            status text NOT NULL,
            requested_by uuid NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            decided_by uuid,
            decided_at timestamptz,
            decision_note text,
            locked_period boolean NOT NULL DEFAULT false,
            idempotency_key text NOT NULL,
            version integer NOT NULL DEFAULT 1,
            CONSTRAINT pk_attendance_corrections PRIMARY KEY (id),
            CONSTRAINT fk_attendance_corrections_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_attendance_corrections_requested_by FOREIGN KEY (requested_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_attendance_corrections_decided_by FOREIGN KEY (decided_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_attendance_corrections_employee_id_idempotency_key UNIQUE (
                employee_id, idempotency_key
            ),
            CONSTRAINT ck_attendance_corrections_kind CHECK (kind IN ('request', 'hr_recorded')),
            CONSTRAINT ck_attendance_corrections_status CHECK (
                status IN ('pending', 'approved', 'rejected', 'cancelled')
            ),
            CONSTRAINT ck_attendance_corrections_reason CHECK (char_length(reason) BETWEEN 1 AND 1000),
            CONSTRAINT ck_attendance_corrections_decision_note CHECK (
                char_length(decision_note) BETWEEN 1 AND 1000
            ),
            CONSTRAINT ck_attendance_corrections_decided CHECK (
                (status IN ('pending', 'cancelled')) = (decided_by IS NULL)
                AND (decided_by IS NULL) = (decided_at IS NULL)
            ),
            CONSTRAINT ck_attendance_corrections_hr_recorded CHECK (kind = 'request' OR status = 'approved'),
            CONSTRAINT ck_attendance_corrections_idempotency_key CHECK (
                char_length(idempotency_key) BETWEEN 8 AND 100
            ),
            CONSTRAINT ck_attendance_corrections_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_attendance_corrections_pending ON attendance.attendance_corrections "
        "(employee_id, work_date) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_attendance_corrections_status_work_date "
        "ON attendance.attendance_corrections (status, work_date)"
    )
    op.execute(
        "CREATE INDEX ix_attendance_corrections_employee_id_work_date "
        "ON attendance.attendance_corrections (employee_id, work_date)"
    )
    op.execute(
        """
        CREATE FUNCTION attendance.guard_corrections() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status <> 'pending' OR NEW.status = 'pending'
                   OR (to_jsonb(NEW) - 'status' - 'decided_by' - 'decided_at' - 'decision_note' - 'version')
                      IS DISTINCT FROM
                      (to_jsonb(OLD) - 'status' - 'decided_by' - 'decided_at' - 'decision_note' - 'version') THEN
                    RAISE EXCEPTION 'a correction is decided or cancelled once, and nothing else changes';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'attendance corrections are kept: % is not allowed', TG_OP;
        END
        $$
        """  # noqa: E501 - PostgreSQL keeps a function body as written
    )
    _guard("attendance_corrections", "guard_corrections")

    op.execute(
        """
        CREATE TABLE attendance.attendance_correction_items (
            id uuid NOT NULL DEFAULT uuidv7(),
            correction_id uuid NOT NULL,
            action text NOT NULL,
            event_type text,
            proposed_at timestamptz,
            target_event_id uuid,
            CONSTRAINT pk_attendance_correction_items PRIMARY KEY (id),
            CONSTRAINT fk_attendance_correction_items_correction_id FOREIGN KEY (correction_id)
                REFERENCES attendance.attendance_corrections (id) ON DELETE RESTRICT,
            CONSTRAINT fk_attendance_correction_items_target_event_id FOREIGN KEY (target_event_id)
                REFERENCES attendance.attendance_events (id) ON DELETE RESTRICT,
            CONSTRAINT ck_attendance_correction_items_action CHECK (
                (action = 'add' AND event_type IN ('CLOCK_IN', 'BREAK_START', 'BREAK_END', 'CLOCK_OUT')
                    AND proposed_at IS NOT NULL AND target_event_id IS NULL)
                OR (action = 'void' AND event_type IS NULL AND proposed_at IS NULL
                    AND target_event_id IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_attendance_correction_items_correction_id "
        "ON attendance.attendance_correction_items (correction_id)"
    )
    _guard("attendance_correction_items", "guard_append_only")

    op.execute("ALTER TABLE attendance.attendance_events ADD COLUMN correction_id uuid")
    op.execute(
        "ALTER TABLE attendance.attendance_events ADD CONSTRAINT fk_attendance_events_correction_id "
        "FOREIGN KEY (correction_id) REFERENCES attendance.attendance_corrections (id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE attendance.attendance_events ADD CONSTRAINT ck_attendance_events_correction "
        "CHECK ((source = 'correction') = (correction_id IS NOT NULL))"
    )

    op.execute(
        """
        CREATE TABLE attendance.attendance_event_voids (
            id uuid NOT NULL DEFAULT uuidv7(),
            event_id uuid NOT NULL,
            correction_id uuid NOT NULL,
            voided_by uuid NOT NULL,
            voided_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_attendance_event_voids PRIMARY KEY (id),
            CONSTRAINT uq_attendance_event_voids_event_id UNIQUE (event_id),
            CONSTRAINT fk_attendance_event_voids_event_id FOREIGN KEY (event_id)
                REFERENCES attendance.attendance_events (id) ON DELETE RESTRICT,
            CONSTRAINT fk_attendance_event_voids_correction_id FOREIGN KEY (correction_id)
                REFERENCES attendance.attendance_corrections (id) ON DELETE RESTRICT,
            CONSTRAINT fk_attendance_event_voids_voided_by FOREIGN KEY (voided_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT
        )
        """
    )
    _guard("attendance_event_voids", "guard_append_only")

    op.execute(
        """
        CREATE TABLE attendance.attendance_locks (
            id uuid NOT NULL DEFAULT uuidv7(),
            period_start date NOT NULL,
            period_end date NOT NULL,
            reason text NOT NULL,
            locked_by uuid NOT NULL,
            locked_at timestamptz NOT NULL DEFAULT now(),
            released_by uuid,
            released_at timestamptz,
            release_reason text,
            CONSTRAINT pk_attendance_locks PRIMARY KEY (id),
            CONSTRAINT fk_attendance_locks_locked_by FOREIGN KEY (locked_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_attendance_locks_released_by FOREIGN KEY (released_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_attendance_locks_range CHECK (period_end >= period_start),
            CONSTRAINT ck_attendance_locks_reason CHECK (char_length(reason) BETWEEN 1 AND 500),
            CONSTRAINT ck_attendance_locks_release CHECK (
                (released_at IS NULL AND released_by IS NULL AND release_reason IS NULL)
                OR (released_at IS NOT NULL AND released_by IS NOT NULL
                    AND char_length(release_reason) BETWEEN 1 AND 500)
            ),
            CONSTRAINT ex_attendance_locks_period EXCLUDE USING gist (
                daterange(period_start, period_end, '[]') WITH &&
            ) WHERE (released_at IS NULL)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION attendance.guard_locks() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.released_at IS NOT NULL
                   OR (to_jsonb(NEW) - 'released_by' - 'released_at' - 'release_reason')
                      IS DISTINCT FROM (to_jsonb(OLD) - 'released_by' - 'released_at' - 'release_reason') THEN
                    RAISE EXCEPTION 'an attendance lock is released once, and nothing else changes';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'attendance locks are kept: % is not allowed', TG_OP;
        END
        $$
        """
    )
    _guard("attendance_locks", "guard_locks")

    _outbox_templates((*TEMPLATES_BEFORE, "attendance.correction"))


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DELETE FROM notify.email_outbox WHERE template = 'attendance.correction'")
    _outbox_templates(TEMPLATES_BEFORE)
    op.execute("DROP TABLE attendance.attendance_locks")
    op.execute("DROP FUNCTION attendance.guard_locks()")
    op.execute("DROP TABLE attendance.attendance_event_voids")
    op.execute("ALTER TABLE attendance.attendance_events DROP CONSTRAINT ck_attendance_events_correction")
    op.execute("ALTER TABLE attendance.attendance_events DROP CONSTRAINT fk_attendance_events_correction_id")
    op.execute("ALTER TABLE attendance.attendance_events DROP COLUMN correction_id")
    op.execute("DROP TABLE attendance.attendance_correction_items")
    op.execute("DROP TABLE attendance.attendance_corrections")
    op.execute("DROP FUNCTION attendance.guard_corrections()")
    op.execute("DROP TABLE attendance.attendance_policies")
    op.execute("DROP TABLE attendance.shift_assignments")
    op.execute("DROP TABLE attendance.location_default_shifts")
    op.execute("DROP TABLE attendance.shift_versions")
    op.execute("DROP TABLE attendance.shifts")
    op.execute("DROP FUNCTION attendance.guard_append_only()")
    op.execute("DROP FUNCTION attendance.guard_versioned()")
