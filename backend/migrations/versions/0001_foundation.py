"""Extensions, schemas, default privileges and per-role session limits.

Revision ID: 0001
Revises:

Grants follow docs/database-design.md §3:
- hrms_app and hrms_worker get DML on tables created later in the module schemas.
- In `audit`, every role gets SELECT only by default. INSERT on the audit tables is granted
  table by table in the audit revision, so UPDATE and DELETE are never granted by accident.
- Functions created by the migrator are not executable by PUBLIC; execution is granted
  explicitly where needed.

Session limits bound how long any statement or transaction can run. The audit sealer relies
on transaction_timeout to know when a month partition can no longer receive rows.
"""

from collections.abc import Sequence

from alembic import op

from migrations.support import (
    APP_ROLE,
    AUDIT_RETENTION_ROLE,
    RUNTIME_ROLES,
    WORKER_ROLE,
    quote_ident,
    reset_role_setting,
    set_role_setting,
)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("citext", "btree_gist", "pg_trgm")
MODULE_SCHEMAS = ("identity", "access", "org", "people", "notify", "app")
AUDIT_SCHEMA = "audit"
AUDIT_READERS = (APP_ROLE, WORKER_ROLE, AUDIT_RETENTION_ROLE)

ROLE_SETTINGS: dict[str, dict[str, str]] = {
    APP_ROLE: {
        "statement_timeout": "5s",
        "transaction_timeout": "30s",
        "idle_in_transaction_session_timeout": "10s",
    },
    WORKER_ROLE: {
        "statement_timeout": "5min",
        "transaction_timeout": "10min",
    },
    # The retention job runs in the worker process; it gets the worker's limits.
    AUDIT_RETENTION_ROLE: {
        "statement_timeout": "5min",
        "transaction_timeout": "10min",
    },
}


def _roles(roles: Sequence[str]) -> str:
    return ", ".join(quote_ident(role) for role in roles)


def upgrade() -> None:
    for extension in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {quote_ident(extension)}")

    op.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")

    for schema in MODULE_SCHEMAS:
        name = quote_ident(schema)
        op.execute(f"CREATE SCHEMA {name}")
        op.execute(f"GRANT USAGE ON SCHEMA {name} TO {_roles(RUNTIME_ROLES)}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {name} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_roles(RUNTIME_ROLES)}"
        )

    audit = quote_ident(AUDIT_SCHEMA)
    op.execute(f"CREATE SCHEMA {audit}")
    op.execute(f"GRANT USAGE ON SCHEMA {audit} TO {_roles(AUDIT_READERS)}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {audit} GRANT SELECT ON TABLES TO {_roles(AUDIT_READERS)}"
    )

    for role, settings in ROLE_SETTINGS.items():
        for parameter, value in settings.items():
            set_role_setting(role, parameter, value)


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    for role, settings in ROLE_SETTINGS.items():
        for parameter in settings:
            reset_role_setting(role, parameter)

    audit = quote_ident(AUDIT_SCHEMA)
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {audit} REVOKE SELECT ON TABLES FROM {_roles(AUDIT_READERS)}"
    )
    op.execute(f"DROP SCHEMA {audit}")

    for schema in reversed(MODULE_SCHEMAS):
        name = quote_ident(schema)
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {name} "
            f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_roles(RUNTIME_ROLES)}"
        )
        op.execute(f"DROP SCHEMA {name}")

    op.execute("ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO PUBLIC")

    for extension in reversed(EXTENSIONS):
        op.execute(f"DROP EXTENSION IF EXISTS {quote_ident(extension)}")
