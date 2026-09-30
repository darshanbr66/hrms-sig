"""Database roles, grants and session limits after `upgrade head` (docs/database-design.md §3)."""

import psycopg
import pytest

from tests.conftest import PostgresServer

MODULE_SCHEMAS = ("identity", "access", "org", "people", "notify", "app")
ALL_SCHEMAS = (*MODULE_SCHEMAS, "audit", "procrastinate", "public")


def show(postgres: PostgresServer, role: str, parameter: str) -> str:
    with postgres.connect(role) as connection:
        row = connection.execute(f"SHOW {parameter}").fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            "hrms_app",
            {
                "statement_timeout": "5s",
                "transaction_timeout": "30s",
                "idle_in_transaction_session_timeout": "10s",
                "search_path": "procrastinate, public",
            },
        ),
        (
            "hrms_worker",
            {
                "statement_timeout": "5min",
                "transaction_timeout": "10min",
                "search_path": "procrastinate, public",
            },
        ),
        ("hrms_audit_retention", {"statement_timeout": "5min", "transaction_timeout": "10min"}),
    ],
)
def test_role_session_limits(migrated_postgres: PostgresServer, role: str, expected: dict[str, str]) -> None:
    for parameter, value in expected.items():
        assert show(migrated_postgres, role, parameter) == value


def test_migrator_owns_every_schema(migrated_postgres: PostgresServer) -> None:
    with migrated_postgres.connect("postgres") as connection:
        rows = connection.execute(
            "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = ANY(%s)",
            (list(ALL_SCHEMAS),),
        ).fetchall()
    # `public` belongs to pg_database_owner, which is the migrator because it owns the database.
    assert dict(rows) == {**dict.fromkeys(ALL_SCHEMAS, "hrms_migrator"), "public": "pg_database_owner"}
    with migrated_postgres.connect("postgres") as connection:
        owner = connection.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        ).fetchone()
    assert owner == ("hrms_migrator",)


@pytest.mark.parametrize("role", ["hrms_app", "hrms_worker", "hrms_audit_retention"])
@pytest.mark.parametrize("schema", ALL_SCHEMAS)
def test_runtime_roles_cannot_create_objects(
    migrated_postgres: PostgresServer, role: str, schema: str
) -> None:
    with migrated_postgres.connect(role) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute(f"CREATE TABLE {schema}.intruder (id int)")


@pytest.mark.parametrize("role", ["hrms_app", "hrms_worker", "hrms_audit_retention"])
def test_runtime_roles_cannot_create_temporary_tables(migrated_postgres: PostgresServer, role: str) -> None:
    with migrated_postgres.connect(role) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute("CREATE TEMPORARY TABLE scratch (id int)")


def test_audit_retention_role_has_no_access_to_module_schemas(migrated_postgres: PostgresServer) -> None:
    with migrated_postgres.connect("postgres") as connection:
        for schema in (*MODULE_SCHEMAS, "procrastinate"):
            row = connection.execute(
                "SELECT has_schema_privilege('hrms_audit_retention', %s, 'USAGE')", (schema,)
            ).fetchone()
            assert row == (False,), schema


def default_acl(postgres: PostgresServer) -> dict[tuple[str, str], str]:
    """(schema, object type) -> privileges string for tables created later by the migrator."""
    with postgres.connect("postgres") as connection:
        rows = connection.execute(
            """
            SELECT coalesce(n.nspname, '*'), d.defaclobjtype, d.defaclacl::text
            FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
            WHERE pg_get_userbyid(d.defaclrole) = 'hrms_migrator'
            """
        ).fetchall()
    return {(schema, kind): acl for schema, kind, acl in rows}


def test_future_module_tables_get_dml_for_runtime_roles_only(migrated_postgres: PostgresServer) -> None:
    acl = default_acl(migrated_postgres)
    for schema in MODULE_SCHEMAS:
        entries = acl[(schema, "r")]
        assert "hrms_app=arwd/hrms_migrator" in entries
        assert "hrms_worker=arwd/hrms_migrator" in entries
        assert "hrms_audit_retention" not in entries


def test_future_audit_tables_are_read_only_by_default(migrated_postgres: PostgresServer) -> None:
    entries = default_acl(migrated_postgres)[("audit", "r")]
    for role in ("hrms_app", "hrms_worker", "hrms_audit_retention"):
        assert f"{role}=r/hrms_migrator" in entries


def test_new_functions_are_not_executable_by_public(migrated_postgres: PostgresServer) -> None:
    # A global entry whose ACL omits PUBLIC ("=X/...") means PUBLIC's default EXECUTE is revoked.
    global_functions = default_acl(migrated_postgres)[("*", "f")]
    assert "=X/" not in global_functions.replace("hrms_migrator=X/", "")
