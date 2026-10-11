"""Leave types, effective-dated leave policies, the leave ledger, requests and decisions
(docs/database-design.md §4.6, product-requirements.md §6.5).

Revision ID: 0018
Revises: 0017

Expand only, in the `leave` schema of revision 0016 (whose default privileges already grant the
runtime roles):

- `leave_types`: Sigvitas's catalogue. Nothing is seeded; every rule column is NOT NULL without
  a default, so each value is a decision HR enters, never one the schema assumes.
- `leave_policies`: effective-dated rules per leave type, optionally narrowed to a location
  and/or an employment type. Ranges of the same scope cannot overlap (EXCLUDE). A row is never
  edited: only an open `effective_to` may be set, when a newer version takes over (trigger).
- `leave_ledger`: the balance is the sum of its entries (LV-2). Append-only (trigger). One
  upfront allocation and one opening balance per employee, leave type and leave year; one
  usage and one reversal per request; one adjustment per idempotency key.
- `leave_requests`: active requests of one employee cannot overlap (LV-4, EXCLUDE).
  `leave_request_days` records how each day was counted; `leave_decisions` is append-only and
  its idempotency key makes a retried decision return the first answer.
- The `leave.request` email template on the outbox.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
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
)
EMPLOYMENT_TYPES = "'full_time', 'part_time', 'contract', 'intern'"
NO_LOCATION = "'00000000-0000-0000-0000-000000000000'::uuid"


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
        f"CREATE TRIGGER {function}_rows BEFORE UPDATE OR DELETE ON leave.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION leave.{function}()"
    )
    op.execute(
        f"CREATE TRIGGER {function}_truncate BEFORE TRUNCATE ON leave.{table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION leave.{function}()"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE leave.leave_types (
            id uuid NOT NULL DEFAULT uuidv7(),
            code citext NOT NULL,
            name text NOT NULL,
            description text,
            is_paid boolean NOT NULL,
            requires_approval boolean NOT NULL,
            allows_half_day boolean NOT NULL,
            counts_weekly_offs boolean NOT NULL,
            counts_holidays boolean NOT NULL,
            status text NOT NULL DEFAULT 'active',
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_leave_types PRIMARY KEY (id),
            CONSTRAINT uq_leave_types_code UNIQUE (code),
            CONSTRAINT ck_leave_types_code CHECK (char_length(code) BETWEEN 1 AND 32 AND code !~ '\\s'),
            CONSTRAINT ck_leave_types_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_leave_types_description CHECK (char_length(description) BETWEEN 1 AND 1000),
            CONSTRAINT ck_leave_types_status CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_leave_types_version CHECK (version >= 1)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE leave.leave_policies (
            id uuid NOT NULL DEFAULT uuidv7(),
            leave_type_id uuid NOT NULL,
            location_id uuid,
            employment_type text,
            effective_from date NOT NULL,
            effective_to date,
            leave_year_start_month smallint NOT NULL,
            leave_year_start_day smallint NOT NULL,
            accrual_method text NOT NULL,
            accrual_amount numeric(5, 2),
            allow_negative_balance boolean NOT NULL,
            max_negative_days numeric(5, 2),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_leave_policies PRIMARY KEY (id),
            CONSTRAINT fk_leave_policies_leave_type_id FOREIGN KEY (leave_type_id)
                REFERENCES leave.leave_types (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_policies_location_id FOREIGN KEY (location_id)
                REFERENCES org.locations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_policies_created_by FOREIGN KEY (created_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_leave_policies_employment_type CHECK (employment_type IN ({EMPLOYMENT_TYPES})),
            CONSTRAINT ck_leave_policies_range CHECK (effective_to IS NULL OR effective_to > effective_from),
            CONSTRAINT ck_leave_policies_year_start CHECK (
                leave_year_start_month BETWEEN 1 AND 12 AND leave_year_start_day BETWEEN 1 AND 31
            ),
            CONSTRAINT ck_leave_policies_accrual CHECK (
                (accrual_method = 'none' AND accrual_amount IS NULL)
                OR (accrual_method = 'upfront_per_leave_year' AND accrual_amount > 0)
            ),
            CONSTRAINT ck_leave_policies_negative CHECK (
                (allow_negative_balance AND max_negative_days >= 0)
                OR (NOT allow_negative_balance AND max_negative_days IS NULL)
            ),
            CONSTRAINT ex_leave_policies_scope_effective EXCLUDE USING gist (
                leave_type_id WITH =,
                (coalesce(location_id, {NO_LOCATION})) WITH =,
                (coalesce(employment_type, '')) WITH =,
                daterange(effective_from, effective_to) WITH &&
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_leave_policies_leave_type_id ON leave.leave_policies (leave_type_id)")
    op.execute(
        """
        CREATE FUNCTION leave.guard_policies() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.effective_to IS NOT NULL
                   OR (to_jsonb(NEW) - 'effective_to' - 'updated_at')
                      IS DISTINCT FROM (to_jsonb(OLD) - 'effective_to' - 'updated_at') THEN
                    RAISE EXCEPTION 'leave policies are versioned: only an open effective_to can be set'
                        USING HINT = 'Create a new policy version instead.';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'leave policies are versioned: % is not allowed on %', TG_OP, TG_TABLE_NAME;
        END
        $$
        """
    )
    _guard("leave_policies", "guard_policies")

    op.execute(
        """
        CREATE TABLE leave.leave_requests (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            leave_type_id uuid NOT NULL,
            start_date date NOT NULL,
            end_date date NOT NULL,
            start_half text,
            end_half text,
            days_requested numeric(5, 2) NOT NULL,
            reason text,
            status text NOT NULL,
            submitted_by uuid NOT NULL,
            idempotency_key text NOT NULL,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_leave_requests PRIMARY KEY (id),
            CONSTRAINT fk_leave_requests_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_requests_leave_type_id FOREIGN KEY (leave_type_id)
                REFERENCES leave.leave_types (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_requests_submitted_by FOREIGN KEY (submitted_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_leave_requests_employee_id_idempotency_key UNIQUE (employee_id, idempotency_key),
            CONSTRAINT ck_leave_requests_range CHECK (end_date >= start_date),
            CONSTRAINT ck_leave_requests_start_half CHECK (start_half IN ('first', 'second')),
            CONSTRAINT ck_leave_requests_end_half CHECK (end_half IN ('first', 'second')),
            CONSTRAINT ck_leave_requests_days CHECK (days_requested > 0),
            CONSTRAINT ck_leave_requests_reason CHECK (char_length(reason) BETWEEN 1 AND 1000),
            CONSTRAINT ck_leave_requests_status CHECK (
                status IN ('pending', 'approved', 'rejected', 'cancelled', 'cancellation_requested')
            ),
            CONSTRAINT ck_leave_requests_idempotency_key CHECK (
                char_length(idempotency_key) BETWEEN 8 AND 100
            ),
            CONSTRAINT ck_leave_requests_version CHECK (version >= 1),
            CONSTRAINT ex_leave_requests_employee_id_dates EXCLUDE USING gist (
                employee_id WITH =,
                daterange(start_date, end_date, '[]') WITH &&
            ) WHERE (status IN ('pending', 'approved', 'cancellation_requested'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_leave_requests_status_start_date ON leave.leave_requests (status, start_date)"
    )
    op.execute("CREATE INDEX ix_leave_requests_leave_type_id ON leave.leave_requests (leave_type_id)")
    op.execute(
        "CREATE INDEX ix_leave_requests_employee_id_start_date "
        "ON leave.leave_requests (employee_id, start_date)"
    )

    op.execute(
        """
        CREATE TABLE leave.leave_request_days (
            leave_request_id uuid NOT NULL,
            date date NOT NULL,
            portion numeric(3, 2) NOT NULL,
            counted boolean NOT NULL,
            not_counted_reason text,
            CONSTRAINT pk_leave_request_days PRIMARY KEY (leave_request_id, date),
            CONSTRAINT fk_leave_request_days_leave_request_id FOREIGN KEY (leave_request_id)
                REFERENCES leave.leave_requests (id) ON DELETE CASCADE,
            CONSTRAINT ck_leave_request_days_portion CHECK (portion IN (0.5, 1.0)),
            CONSTRAINT ck_leave_request_days_reason CHECK (
                (counted AND not_counted_reason IS NULL)
                OR (NOT counted AND not_counted_reason IN ('holiday', 'weekly_off'))
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE leave.leave_decisions (
            id uuid NOT NULL DEFAULT uuidv7(),
            leave_request_id uuid NOT NULL,
            step smallint NOT NULL DEFAULT 1,
            decision text NOT NULL,
            decided_by uuid NOT NULL,
            note text,
            idempotency_key text NOT NULL,
            decided_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_leave_decisions PRIMARY KEY (id),
            CONSTRAINT fk_leave_decisions_leave_request_id FOREIGN KEY (leave_request_id)
                REFERENCES leave.leave_requests (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_decisions_decided_by FOREIGN KEY (decided_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_leave_decisions_decided_by_idempotency_key UNIQUE (decided_by, idempotency_key),
            CONSTRAINT ck_leave_decisions_decision CHECK (
                decision IN ('approved', 'rejected', 'cancellation_approved', 'cancellation_rejected')
            ),
            CONSTRAINT ck_leave_decisions_step CHECK (step >= 1),
            CONSTRAINT ck_leave_decisions_note CHECK (char_length(note) BETWEEN 1 AND 1000),
            CONSTRAINT ck_leave_decisions_idempotency_key CHECK (
                char_length(idempotency_key) BETWEEN 8 AND 100
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_leave_decisions_leave_request_id ON leave.leave_decisions (leave_request_id)")

    op.execute(
        """
        CREATE TABLE leave.leave_ledger (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            leave_type_id uuid NOT NULL,
            leave_year date NOT NULL,
            entry_type text NOT NULL,
            amount numeric(5, 2) NOT NULL,
            effective_date date NOT NULL,
            leave_request_id uuid,
            policy_id uuid,
            reason text,
            created_by uuid NOT NULL,
            idempotency_key text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_leave_ledger PRIMARY KEY (id),
            CONSTRAINT fk_leave_ledger_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_ledger_leave_type_id FOREIGN KEY (leave_type_id)
                REFERENCES leave.leave_types (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_ledger_leave_request_id FOREIGN KEY (leave_request_id)
                REFERENCES leave.leave_requests (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_ledger_policy_id FOREIGN KEY (policy_id)
                REFERENCES leave.leave_policies (id) ON DELETE RESTRICT,
            CONSTRAINT fk_leave_ledger_created_by FOREIGN KEY (created_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_leave_ledger_entry_type CHECK (
                entry_type IN ('opening_balance', 'accrual', 'usage', 'usage_reversal', 'adjustment')
            ),
            CONSTRAINT ck_leave_ledger_amount CHECK (amount <> 0 AND amount * 2 = round(amount * 2)),
            CONSTRAINT ck_leave_ledger_usage CHECK (
                (entry_type IN ('usage', 'usage_reversal')) = (leave_request_id IS NOT NULL)
            ),
            CONSTRAINT ck_leave_ledger_sign CHECK (
                (entry_type = 'usage' AND amount < 0)
                OR (entry_type IN ('usage_reversal', 'accrual') AND amount > 0)
                OR entry_type IN ('opening_balance', 'adjustment')
            ),
            CONSTRAINT ck_leave_ledger_reason CHECK (char_length(reason) BETWEEN 1 AND 500),
            CONSTRAINT ck_leave_ledger_adjustment_reason CHECK (
                entry_type NOT IN ('adjustment', 'opening_balance') OR reason IS NOT NULL
            ),
            CONSTRAINT ck_leave_ledger_idempotency_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_leave_ledger_employee_id_leave_type_id_effective_date "
        "ON leave.leave_ledger (employee_id, leave_type_id, effective_date)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_leave_ledger_yearly ON leave.leave_ledger "
        "(employee_id, leave_type_id, leave_year, entry_type) "
        "WHERE entry_type IN ('accrual', 'opening_balance')"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_leave_ledger_request ON leave.leave_ledger (leave_request_id, entry_type) "
        "WHERE leave_request_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_leave_ledger_employee_id_idempotency_key ON leave.leave_ledger "
        "(employee_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    op.execute(
        """
        CREATE FUNCTION leave.guard_append_only() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING HINT = 'Record a new entry instead.';
        END
        $$
        """
    )
    _guard("leave_ledger", "guard_append_only")
    op.execute(
        "CREATE TRIGGER guard_decisions_rows BEFORE UPDATE OR DELETE ON leave.leave_decisions "
        "FOR EACH ROW EXECUTE FUNCTION leave.guard_append_only()"
    )
    op.execute(
        "CREATE TRIGGER guard_decisions_truncate BEFORE TRUNCATE ON leave.leave_decisions "
        "FOR EACH STATEMENT EXECUTE FUNCTION leave.guard_append_only()"
    )

    _outbox_templates((*TEMPLATES_BEFORE, "leave.request"))


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DELETE FROM notify.email_outbox WHERE template = 'leave.request'")
    _outbox_templates(TEMPLATES_BEFORE)
    op.execute("DROP TABLE leave.leave_ledger")
    op.execute("DROP TABLE leave.leave_decisions")
    op.execute("DROP TABLE leave.leave_request_days")
    op.execute("DROP TABLE leave.leave_requests")
    op.execute("DROP TABLE leave.leave_policies")
    op.execute("DROP FUNCTION leave.guard_append_only()")
    op.execute("DROP FUNCTION leave.guard_policies()")
    op.execute("DROP TABLE leave.leave_types")
