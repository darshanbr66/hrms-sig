"""Emergency contacts (docs/database-design.md §4.4, product-requirements EMP-3).

Revision ID: 0024
Revises: 0023

Expand only, in the `people` schema. `people.emergency_contacts` belongs to the `personal`
tier: read with `employee.personal.read` (self or all) or `employee.emergency_contact.read.team`
(granted to no role by default), changed with `employee.personal.update`. A person keeps up to
five contacts in priority order; the list is replaced as a whole.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE people.emergency_contacts (
            id uuid NOT NULL DEFAULT uuidv7(),
            employee_id uuid NOT NULL,
            priority smallint NOT NULL,
            name text NOT NULL,
            relationship text NOT NULL,
            phone text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_emergency_contacts PRIMARY KEY (id),
            CONSTRAINT fk_emergency_contacts_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT uq_emergency_contacts_employee_id_priority UNIQUE (employee_id, priority),
            CONSTRAINT ck_emergency_contacts_priority CHECK (priority BETWEEN 1 AND 5),
            CONSTRAINT ck_emergency_contacts_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_emergency_contacts_relationship CHECK (char_length(relationship) BETWEEN 1 AND 100),
            CONSTRAINT ck_emergency_contacts_phone CHECK (phone ~ '^\\+?[0-9 ()-]{5,30}$')
        )
        """
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE people.emergency_contacts")
