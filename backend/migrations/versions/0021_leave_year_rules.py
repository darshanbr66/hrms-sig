"""Periodic accrual, proration, carry-forward, lapse, expiry and opening-balance imports
(docs/database-design.md §4.6, product-requirements LV-1, LV-2, §6.12).

Revision ID: 0021
Revises: 0020

Expand only. Nothing is seeded and no value is assumed:

- `leave_policies` gains the remaining rule columns. Each is nullable so the policies written
  before this revision stay valid and say "not decided" (for example a policy without a
  carry-forward mode is skipped by the year-end run and listed as needing a decision); new
  policies are validated by the service. CHECKs tie the columns to the accrual method:
  periodic accrual needs a frequency, a timing and both proration choices; proration needs a
  rounding rule; a capped carry-forward needs a cap and an action for the rest.
- `leave_ledger` gains `period_start` (the accrual period a periodic entry belongs to),
  `import_batch_id` (opening balances imported together) and the year-end entry types:
  `carry_forward_out` (leaving the old year), `carry_forward` (arriving in the new one),
  `carry_forward_expiry` and `lapse`. One of each per employee, leave type and leave year;
  one periodic accrual per period. Amounts other than usage may hold quarter or hundredth days
  (accrual of 1.75 days a month, prorated amounts); usage stays in half days.
- `created_by` may be empty only for accrual and expiry entries the worker writes as the system.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_TYPES_BEFORE = ("opening_balance", "accrual", "usage", "usage_reversal", "adjustment")
ENTRY_TYPES = (*ENTRY_TYPES_BEFORE, "carry_forward_out", "carry_forward", "carry_forward_expiry", "lapse")
YEARLY = ("opening_balance", "carry_forward_out", "carry_forward", "carry_forward_expiry", "lapse")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE leave.leave_policies
            ADD COLUMN accrual_frequency text,
            ADD COLUMN accrual_timing text,
            ADD COLUMN prorate_on_joining boolean,
            ADD COLUMN prorate_on_exit boolean,
            ADD COLUMN proration_rounding text,
            ADD COLUMN carry_forward_mode text,
            ADD COLUMN carry_forward_cap numeric(5, 2),
            ADD COLUMN excess_action text,
            ADD COLUMN carried_expire_after_months smallint
        """
    )
    op.execute("ALTER TABLE leave.leave_policies DROP CONSTRAINT ck_leave_policies_accrual")
    op.execute(
        """
        ALTER TABLE leave.leave_policies ADD CONSTRAINT ck_leave_policies_accrual CHECK (
            (accrual_method = 'none' AND accrual_amount IS NULL AND accrual_frequency IS NULL
                AND accrual_timing IS NULL AND prorate_on_joining IS NULL AND prorate_on_exit IS NULL)
            OR (accrual_method = 'upfront_per_leave_year' AND accrual_amount > 0 AND accrual_frequency IS NULL
                AND accrual_timing IS NULL AND prorate_on_exit IS NULL)
            OR (accrual_method = 'periodic' AND accrual_amount > 0
                AND accrual_frequency IN ('monthly', 'quarterly')
                AND accrual_timing IN ('period_start', 'period_end')
                AND prorate_on_joining IS NOT NULL AND prorate_on_exit IS NOT NULL)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE leave.leave_policies ADD CONSTRAINT ck_leave_policies_proration CHECK (
            (coalesce(prorate_on_joining, false) OR coalesce(prorate_on_exit, false))
                = (proration_rounding IS NOT NULL)
            AND (proration_rounding IS NULL
                OR proration_rounding IN ('exact', 'nearest_half', 'down_half', 'up_half'))
        )
        """
    )
    op.execute(
        """
        ALTER TABLE leave.leave_policies ADD CONSTRAINT ck_leave_policies_carry_forward CHECK (
            (carry_forward_mode IS NULL AND carry_forward_cap IS NULL AND excess_action IS NULL
                AND carried_expire_after_months IS NULL)
            OR (carry_forward_mode = 'none' AND carry_forward_cap IS NULL
                AND excess_action IN ('lapse', 'keep') AND carried_expire_after_months IS NULL)
            OR (carry_forward_mode = 'capped' AND carry_forward_cap >= 0
                AND excess_action IN ('lapse', 'keep'))
            OR (carry_forward_mode = 'unlimited' AND carry_forward_cap IS NULL AND excess_action IS NULL)
        )
        """
    )
    op.execute(
        "ALTER TABLE leave.leave_policies ADD CONSTRAINT ck_leave_policies_carried_expiry "
        "CHECK (carried_expire_after_months BETWEEN 1 AND 24)"
    )
    op.execute(
        "ALTER TABLE leave.leave_policies ADD CONSTRAINT ck_leave_policies_accrual_amount "
        "CHECK (accrual_amount <= 366)"
    )

    op.execute(
        """
        ALTER TABLE leave.leave_ledger
            ADD COLUMN period_start date,
            ADD COLUMN import_batch_id uuid,
            ALTER COLUMN created_by DROP NOT NULL
        """
    )
    op.execute("ALTER TABLE leave.leave_ledger DROP CONSTRAINT ck_leave_ledger_entry_type")
    op.execute(
        f"ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_entry_type "
        f"CHECK (entry_type IN ({_in(ENTRY_TYPES)}))"
    )
    op.execute("ALTER TABLE leave.leave_ledger DROP CONSTRAINT ck_leave_ledger_amount")
    op.execute(
        """
        ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_amount CHECK (
            amount <> 0
            AND (entry_type NOT IN ('usage', 'usage_reversal') OR amount * 2 = round(amount * 2))
        )
        """
    )
    op.execute("ALTER TABLE leave.leave_ledger DROP CONSTRAINT ck_leave_ledger_sign")
    op.execute(
        """
        ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_sign CHECK (
            (entry_type = 'usage' AND amount < 0)
            OR (entry_type IN ('usage_reversal', 'accrual', 'carry_forward') AND amount > 0)
            OR (entry_type IN ('carry_forward_out', 'carry_forward_expiry', 'lapse') AND amount < 0)
            OR entry_type IN ('opening_balance', 'adjustment')
        )
        """
    )
    op.execute(
        """
        ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_period_start CHECK (
            period_start IS NULL OR (entry_type = 'accrual' AND period_start >= leave_year)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_created_by CHECK (
            created_by IS NOT NULL OR entry_type IN ('accrual', 'carry_forward_expiry')
        )
        """
    )
    op.execute(
        """
        ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_import_batch CHECK (
            import_batch_id IS NULL OR entry_type = 'opening_balance'
        )
        """
    )
    op.execute("DROP INDEX leave.uq_leave_ledger_yearly")
    op.execute(
        f"CREATE UNIQUE INDEX uq_leave_ledger_yearly ON leave.leave_ledger "
        f"(employee_id, leave_type_id, leave_year, entry_type) "
        f"WHERE entry_type IN ({_in(YEARLY)}) OR (entry_type = 'accrual' AND period_start IS NULL)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_leave_ledger_periodic ON leave.leave_ledger "
        "(employee_id, leave_type_id, period_start) WHERE entry_type = 'accrual' AND period_start IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_leave_ledger_import_batch_id ON leave.leave_ledger (import_batch_id) "
        "WHERE import_batch_id IS NOT NULL"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production. Refuses (by
    the restored constraints) when entries of the new kinds exist."""
    op.execute("DROP INDEX leave.ix_leave_ledger_import_batch_id")
    op.execute("DROP INDEX leave.uq_leave_ledger_periodic")
    op.execute("DROP INDEX leave.uq_leave_ledger_yearly")
    op.execute(
        "CREATE UNIQUE INDEX uq_leave_ledger_yearly ON leave.leave_ledger "
        "(employee_id, leave_type_id, leave_year, entry_type) "
        "WHERE entry_type IN ('accrual', 'opening_balance')"
    )
    for constraint in ("import_batch", "created_by", "period_start", "sign", "amount", "entry_type"):
        op.execute(f"ALTER TABLE leave.leave_ledger DROP CONSTRAINT ck_leave_ledger_{constraint}")
    op.execute(
        f"ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_entry_type "
        f"CHECK (entry_type IN ({_in(ENTRY_TYPES_BEFORE)}))"
    )
    op.execute(
        "ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_amount "
        "CHECK (amount <> 0 AND amount * 2 = round(amount * 2))"
    )
    op.execute(
        """
        ALTER TABLE leave.leave_ledger ADD CONSTRAINT ck_leave_ledger_sign CHECK (
            (entry_type = 'usage' AND amount < 0)
            OR (entry_type IN ('usage_reversal', 'accrual') AND amount > 0)
            OR entry_type IN ('opening_balance', 'adjustment')
        )
        """
    )
    op.execute(
        """
        ALTER TABLE leave.leave_ledger
            DROP COLUMN period_start,
            DROP COLUMN import_batch_id,
            ALTER COLUMN created_by SET NOT NULL
        """
    )
    for constraint in ("accrual_amount", "carried_expiry", "carry_forward", "proration", "accrual"):
        op.execute(f"ALTER TABLE leave.leave_policies DROP CONSTRAINT ck_leave_policies_{constraint}")
    op.execute(
        """
        ALTER TABLE leave.leave_policies ADD CONSTRAINT ck_leave_policies_accrual CHECK (
            (accrual_method = 'none' AND accrual_amount IS NULL)
            OR (accrual_method = 'upfront_per_leave_year' AND accrual_amount > 0)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE leave.leave_policies
            DROP COLUMN accrual_frequency,
            DROP COLUMN accrual_timing,
            DROP COLUMN prorate_on_joining,
            DROP COLUMN prorate_on_exit,
            DROP COLUMN proration_rounding,
            DROP COLUMN carry_forward_mode,
            DROP COLUMN carry_forward_cap,
            DROP COLUMN excess_action,
            DROP COLUMN carried_expire_after_months
        """
    )
