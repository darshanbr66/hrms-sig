"""Organization tables: locations, departments, designations (docs/database-design.md §4.3).

Revision ID: 0003
Revises: 0002

Expand/contract: new tables only.

Tables only in M1: they exist so `people.employee_jobs` can reference them. HR creates the
real structure through the M2 screens; nothing is seeded (docs/database-design.md §10).
`departments.head_employee_id` references `people.employees`, which revision 0004 creates,
so that foreign key is added there.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE org.locations (
            id uuid NOT NULL DEFAULT uuidv7(),
            code citext NOT NULL,
            name text NOT NULL,
            time_zone text NOT NULL,
            country_code char(2) NOT NULL,
            address text,
            status text NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_locations PRIMARY KEY (id),
            CONSTRAINT uq_locations_code UNIQUE (code),
            CONSTRAINT ck_locations_code CHECK (char_length(code) BETWEEN 1 AND 32),
            CONSTRAINT ck_locations_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_locations_time_zone CHECK (char_length(time_zone) BETWEEN 1 AND 64),
            CONSTRAINT ck_locations_country_code CHECK (country_code ~ '^[A-Z]{2}$'),
            CONSTRAINT ck_locations_address CHECK (char_length(address) <= 1000),
            CONSTRAINT ck_locations_status CHECK (status IN ('active', 'archived'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE org.departments (
            id uuid NOT NULL DEFAULT uuidv7(),
            code citext NOT NULL,
            name text NOT NULL,
            parent_id uuid,
            head_employee_id uuid,
            status text NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_departments PRIMARY KEY (id),
            CONSTRAINT uq_departments_code UNIQUE (code),
            CONSTRAINT fk_departments_parent_id FOREIGN KEY (parent_id)
                REFERENCES org.departments (id) ON DELETE RESTRICT,
            CONSTRAINT ck_departments_code CHECK (char_length(code) BETWEEN 1 AND 32),
            CONSTRAINT ck_departments_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_departments_parent_not_self CHECK (parent_id <> id),
            CONSTRAINT ck_departments_status CHECK (status IN ('active', 'archived'))
        )
        """
    )
    op.execute("CREATE INDEX ix_departments_parent_id ON org.departments (parent_id)")
    op.execute(
        """
        CREATE TABLE org.designations (
            id uuid NOT NULL DEFAULT uuidv7(),
            code citext NOT NULL,
            name text NOT NULL,
            level integer,
            status text NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_designations PRIMARY KEY (id),
            CONSTRAINT uq_designations_code UNIQUE (code),
            CONSTRAINT ck_designations_code CHECK (char_length(code) BETWEEN 1 AND 32),
            CONSTRAINT ck_designations_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_designations_level CHECK (level >= 0),
            CONSTRAINT ck_designations_status CHECK (status IN ('active', 'archived'))
        )
        """
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE org.designations")
    op.execute("DROP TABLE org.departments")
    op.execute("DROP TABLE org.locations")
