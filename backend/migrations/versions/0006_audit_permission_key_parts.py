"""Accept two-part permission keys in audit_log.permission_used.

Revision ID: 0006
Revises: 0005

Revision 0005 required three or four dot-separated parts, but the permission catalog has
keys whose domain has one main resource and so omits it (`user.invite`, `org.read`,
docs/authorization-model.md §2). Those keys could not be recorded. The constraint is
replaced with one that accepts two to four parts.

Expand/contract: the new constraint is weaker than the old one, so every existing row
satisfies it. Re-adding a CHECK on the partitioned table scans each partition once under a
lock; the audit tables hold no production data yet, so this is instant. The partition
maintenance function copies the parent's constraints, so new partitions get the new one.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_audit_log_permission_used"
TWO_TO_FOUR_PARTS = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$"
THREE_TO_FOUR_PARTS = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,3}$"


def _replace(pattern: str) -> None:
    op.execute(f"ALTER TABLE audit.audit_log DROP CONSTRAINT {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE audit.audit_log ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK (char_length(permission_used) <= 100 AND permission_used ~ '{pattern}')"
    )


def upgrade() -> None:
    _replace(TWO_TO_FOUR_PARTS)


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    _replace(THREE_TO_FOUR_PARTS)
