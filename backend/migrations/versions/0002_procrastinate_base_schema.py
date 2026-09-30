"""Procrastinate base schema, pinned release 3.10.0 (ADR-009, ADR-024).

Revision ID: 0002
Revises: 0001

Applies the upstream schema.sql unchanged, after verifying it against the recorded
checksum, into the dedicated `procrastinate` schema. The runtime roles find it through their
database-level search_path, which also keeps `public` so extension operators (citext
equality, pg_trgm similarity) resolve.

Later Procrastinate upgrades apply the upstream migration files in their own revisions
(docs/database-design.md §11). The downgrade below exists only so CI can exercise
upgrade → downgrade base → upgrade on an empty database.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path

from alembic import op

from migrations.support import (
    RUNTIME_ROLES,
    grant_table_dml,
    quote_ident,
    reset_role_setting,
    set_role_search_path,
)

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROCRASTINATE_VERSION = "3.10.0"
SCHEMA = "procrastinate"
VENDOR_DIR = Path(__file__).resolve().parents[1] / "vendor" / "procrastinate" / PROCRASTINATE_VERSION
RUNTIME_SEARCH_PATH = ("procrastinate", "public")


def _read_verified(filename: str) -> str:
    expected = {
        name: digest
        for digest, name in (
            line.split() for line in (VENDOR_DIR / "CHECKSUMS").read_text().splitlines() if line
        )
    }
    content = (VENDOR_DIR / filename).read_bytes()
    if hashlib.sha256(content).hexdigest() != expected[filename]:
        raise RuntimeError(f"vendored Procrastinate file {filename} does not match CHECKSUMS")
    return content.decode("utf-8")


def upgrade() -> None:
    sql = _read_verified("schema.sql")
    schema = quote_ident(SCHEMA)
    roles = ", ".join(quote_ident(role) for role in RUNTIME_ROLES)

    op.execute(f"CREATE SCHEMA {schema}")
    op.execute(f"SET LOCAL search_path = {schema}")
    # Executed through the driver without parameters so the file runs byte-for-byte,
    # multiple statements and all, with no placeholder interpretation.
    op.get_bind().connection.driver_connection.execute(sql)  # type: ignore[union-attr]
    op.execute("SET LOCAL search_path TO DEFAULT")

    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {roles}")
    grant_table_dml(SCHEMA)
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {roles}")
    op.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {schema} TO {roles}")

    # Objects added by later Procrastinate migrations get the same access.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {roles}"
    )
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {roles}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT EXECUTE ON FUNCTIONS TO {roles}")

    for role in RUNTIME_ROLES:
        set_role_search_path(role, RUNTIME_SEARCH_PATH)


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    schema = quote_ident(SCHEMA)
    roles = ", ".join(quote_ident(role) for role in RUNTIME_ROLES)
    for role in RUNTIME_ROLES:
        reset_role_setting(role, "search_path")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE EXECUTE ON FUNCTIONS FROM {roles}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE USAGE, SELECT ON SEQUENCES FROM {roles}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {roles}"
    )
    op.execute(f"DROP SCHEMA {schema} CASCADE")
