"""Holiday calendars per location (docs/database-design.md §4.6, product-requirements LV-7).

Revision ID: 0016
Revises: 0015

Expand only: a new `leave` schema with the module-schema grants of revision 0001, and the
holiday tables. No holiday is seeded: Sigvitas HR enters its own calendars. Leave types,
policies, the ledger and requests (the rest of §4.6) need Sigvitas's leave policy values
(roadmap §6) and arrive with them.

- `holiday_calendars`: a named calendar for one year, versioned for optimistic concurrency.
- `holidays`: one row per date in a calendar, inside the calendar's year.
- `location_holiday_calendars`: which calendar a location uses in a year (one per location
  and year, LV-7).
"""

from collections.abc import Sequence

from alembic import op

from migrations.support import RUNTIME_ROLES, quote_ident

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "leave"


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
        CREATE TABLE leave.holiday_calendars (
            id uuid NOT NULL DEFAULT uuidv7(),
            name text NOT NULL,
            year smallint NOT NULL,
            status text NOT NULL DEFAULT 'active',
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_holiday_calendars PRIMARY KEY (id),
            CONSTRAINT uq_holiday_calendars_name_year UNIQUE (name, year),
            CONSTRAINT ck_holiday_calendars_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_holiday_calendars_year CHECK (year BETWEEN 2000 AND 2100),
            CONSTRAINT ck_holiday_calendars_status CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_holiday_calendars_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE leave.holidays (
            id uuid NOT NULL DEFAULT uuidv7(),
            calendar_id uuid NOT NULL,
            date date NOT NULL,
            name text NOT NULL,
            is_optional boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_holidays PRIMARY KEY (id),
            CONSTRAINT fk_holidays_calendar_id FOREIGN KEY (calendar_id)
                REFERENCES leave.holiday_calendars (id) ON DELETE RESTRICT,
            CONSTRAINT uq_holidays_calendar_id_date UNIQUE (calendar_id, date),
            CONSTRAINT ck_holidays_name CHECK (char_length(name) BETWEEN 1 AND 200)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE leave.location_holiday_calendars (
            id uuid NOT NULL DEFAULT uuidv7(),
            location_id uuid NOT NULL,
            calendar_id uuid NOT NULL,
            year smallint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_location_holiday_calendars PRIMARY KEY (id),
            CONSTRAINT fk_location_holiday_calendars_location_id FOREIGN KEY (location_id)
                REFERENCES org.locations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_location_holiday_calendars_calendar_id FOREIGN KEY (calendar_id)
                REFERENCES leave.holiday_calendars (id) ON DELETE RESTRICT,
            CONSTRAINT uq_location_holiday_calendars_location_id_year UNIQUE (location_id, year)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_location_holiday_calendars_calendar_id "
        "ON leave.location_holiday_calendars (calendar_id)"
    )
    # A holiday's date lies in its calendar's year, and a location uses a calendar only for
    # the calendar's own year.
    op.execute(
        """
        CREATE FUNCTION leave.check_holiday_year() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF extract(year FROM NEW.date)
               <> (SELECT year FROM leave.holiday_calendars WHERE id = NEW.calendar_id) THEN
                RAISE EXCEPTION 'holiday date % is outside its calendar year', NEW.date;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION leave.check_location_calendar_year() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.year <> (SELECT year FROM leave.holiday_calendars WHERE id = NEW.calendar_id) THEN
                RAISE EXCEPTION 'a location uses a calendar only for the calendar year';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER check_holiday_year BEFORE INSERT OR UPDATE ON leave.holidays "
        "FOR EACH ROW EXECUTE FUNCTION leave.check_holiday_year()"
    )
    op.execute(
        "CREATE TRIGGER check_location_calendar_year "
        "BEFORE INSERT OR UPDATE ON leave.location_holiday_calendars "
        "FOR EACH ROW EXECUTE FUNCTION leave.check_location_calendar_year()"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE leave.location_holiday_calendars")
    op.execute("DROP TABLE leave.holidays")
    op.execute("DROP FUNCTION leave.check_location_calendar_year()")
    op.execute("DROP FUNCTION leave.check_holiday_year()")
    op.execute("DROP TABLE leave.holiday_calendars")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_roles()}"
    )
    op.execute(f"DROP SCHEMA {SCHEMA}")
