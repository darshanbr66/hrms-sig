"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}

Expand/contract: this revision must work with the code currently deployed and the code
being deployed (docs/database-design.md §11).
"""

from collections.abc import Sequence

from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    ${downgrades if downgrades else "pass"}
