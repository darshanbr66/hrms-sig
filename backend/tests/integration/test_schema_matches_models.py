"""The SQLAlchemy models describe exactly what the migrations create.

Alembic's autogenerate comparison runs against the database at `upgrade head`: tables,
columns, types, nullability, primary keys, unique constraints, foreign keys and indexes
must match. CHECK and exclusion constraints and triggers are covered by the schema tests.
"""

import re
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.metadata import metadata
from app.platform.config import sqlalchemy_url
from tests.conftest import PostgresServer

APPLICATION_SCHEMAS = {
    "identity",
    "access",
    "org",
    "people",
    "notify",
    "app",
    "audit",
    "attendance",
    "leave",
    "docs",
}
# Month partitions are created at runtime (audit.ensure_partitions), and the chain links'
# per-stream partitions hold only those month partitions; neither is modelled.
PARTITION = re.compile(r".+_p\d{4}_\d{2}|chain_links_(audit_log|security_events)")
# Expression indexes with an operator class cannot be compared; checked by name in
# tests/integration/test_people_api.py.
UNCOMPARED_INDEXES = {"ix_employees_search"}


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "schema":
        return name in APPLICATION_SCHEMAS
    if type_ == "table":
        return name is not None and not PARTITION.fullmatch(name)
    if type_ == "index":
        return name not in UNCOMPARED_INDEXES
    return True


def test_models_match_the_migrated_schema(migrated_postgres: PostgresServer) -> None:
    engine = create_engine(sqlalchemy_url(migrated_postgres.url("hrms_migrator")))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_schemas": True,
                    "include_name": include_name,
                    "compare_type": True,
                    "target_metadata": metadata,
                },
            )
            differences = compare_metadata(context, metadata)
    finally:
        engine.dispose()
    assert differences == []
