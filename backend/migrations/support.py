"""Helpers shared by revisions.

Revisions are historical records, so these helpers must stay backward-compatible: change
behaviour only by adding new functions, never by altering what an existing one emits.
"""

from alembic import op
from sqlalchemy import text

APP_ROLE = "hrms_app"
WORKER_ROLE = "hrms_worker"
AUDIT_RETENTION_ROLE = "hrms_audit_retention"
RUNTIME_ROLES = (APP_ROLE, WORKER_ROLE)


def quote_ident(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote_identifier(name)


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def current_database() -> str:
    return str(op.get_bind().execute(text("SELECT current_database()")).scalar_one())


def set_role_setting(role: str, parameter: str, value: str) -> None:
    """Set a per-database session default for a role (ALTER ROLE ... IN DATABASE ... SET)."""
    op.execute(
        f"ALTER ROLE {quote_ident(role)} IN DATABASE {quote_ident(current_database())} "
        f"SET {parameter} = {quote_literal(value)}"
    )


def set_role_search_path(role: str, schemas: tuple[str, ...]) -> None:
    """Set a per-database search_path for a role. Schemas are identifiers, not one literal."""
    op.execute(
        f"ALTER ROLE {quote_ident(role)} IN DATABASE {quote_ident(current_database())} "
        f"SET search_path = {', '.join(quote_ident(schema) for schema in schemas)}"
    )


def reset_role_setting(role: str, parameter: str) -> None:
    op.execute(
        f"ALTER ROLE {quote_ident(role)} IN DATABASE {quote_ident(current_database())} RESET {parameter}"
    )


def grant_table_dml(schema: str, roles: tuple[str, ...] = RUNTIME_ROLES) -> None:
    """Re-assert full DML on every existing table in a module schema."""
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {quote_ident(schema)} "
        f"TO {', '.join(quote_ident(role) for role in roles)}"
    )
