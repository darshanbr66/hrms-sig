"""The permission catalog in the database, its grants, and role-assignment history (revisions 0007-0009)."""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors

from app.platform.authz.catalog import CATALOG
from app.platform.authz.roles import ROLES
from tests.conftest import PostgresServer

CATALOG_TABLES = ("access.permissions", "access.roles", "access.role_permissions")
IDENTITY_TABLES = (
    "identity.users",
    "identity.credentials",
    "identity.mfa_factors",
    "identity.recovery_codes",
    "identity.sessions",
    "identity.session_tokens",
    "identity.one_time_tokens",
    "access.user_roles",
)


@pytest.fixture
def superuser(migrated_postgres: PostgresServer) -> Iterator[psycopg.Connection]:
    with migrated_postgres.connect("postgres") as connection:
        yield connection


def has(connection: psycopg.Connection, role: str, table: str, privilege: str) -> bool:
    row = connection.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)).fetchone()
    return bool(row and row[0])


def test_database_catalog_matches_the_code(superuser: psycopg.Connection) -> None:
    stored = {
        row[0]: row[1:]
        for row in superuser.execute(
            "SELECT key, description, requires_step_up, audit_reads FROM access.permissions"
        ).fetchall()
    }
    assert stored == {p.key: (p.description, p.step_up, p.audit_reads) for p in CATALOG}

    roles = {
        row[0]: (row[1], row[2], row[3], set(row[4] or []))
        for row in superuser.execute(
            "SELECT r.key, r.name, r.description, r.is_derived, array_remove(array_agg(p.key), NULL) "
            "FROM access.roles r LEFT JOIN access.role_permissions rp ON rp.role_id = r.id "
            "LEFT JOIN access.permissions p ON p.id = rp.permission_id "
            "GROUP BY r.key, r.name, r.description, r.is_derived"
        ).fetchall()
    }
    assert roles == {
        role.key.value: (role.name, role.description, role.derived, set(role.permissions)) for role in ROLES
    }


@pytest.mark.parametrize("table", CATALOG_TABLES)
def test_runtime_roles_can_only_read_the_catalog(superuser: psycopg.Connection, table: str) -> None:
    for role in ("hrms_app", "hrms_worker"):
        assert has(superuser, role, table, "SELECT")
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not has(superuser, role, table, privilege), (role, table, privilege)


def test_the_application_cannot_widen_a_role(migrated_postgres: PostgresServer) -> None:
    with migrated_postgres.connect("hrms_app") as connection, pytest.raises(errors.InsufficientPrivilege):
        connection.execute(
            "INSERT INTO access.role_permissions SELECT r.id, p.id FROM access.roles r, access.permissions p "
            "WHERE r.key = 'employee' AND p.key = 'role.assign'"
        )


@pytest.mark.parametrize("table", IDENTITY_TABLES)
def test_identity_tables_grant_dml_but_not_truncate(superuser: psycopg.Connection, table: str) -> None:
    for role in ("hrms_app", "hrms_worker"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert has(superuser, role, table, privilege), (role, table, privilege)
        assert not has(superuser, role, table, "TRUNCATE")
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert not has(superuser, "hrms_audit_retention", table, privilege)


def assignment(connection: psycopg.Connection) -> uuid.UUID:
    user = connection.execute(
        "INSERT INTO identity.users (email, status) VALUES (%s, 'invited') RETURNING id",
        (f"history-{uuid.uuid4().hex[:10]}@dev.example",),
    ).fetchone()
    assert user is not None
    row = connection.execute(
        "INSERT INTO access.user_roles (user_id, role_id, grant_reason) "
        "SELECT %s, id, 'Test' FROM access.roles WHERE key = 'auditor' RETURNING id",
        (user[0],),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def test_role_assignments_are_history(migrated_postgres: PostgresServer) -> None:
    with migrated_postgres.connect("hrms_app") as connection:
        record = assignment(connection)
        for statement in (
            "DELETE FROM access.user_roles WHERE id = %s",
            "UPDATE access.user_roles SET grant_reason = 'Changed' WHERE id = %s",
            "UPDATE access.user_roles SET valid_until = now() + interval '1 day' WHERE id = %s",
        ):
            with pytest.raises(errors.RaiseException, match="history"):
                connection.execute(statement, (record,))
        connection.execute("UPDATE access.user_roles SET revoked_at = now() WHERE id = %s", (record,))
        with pytest.raises(errors.RaiseException, match="cannot change"):
            connection.execute("UPDATE access.user_roles SET revoked_at = NULL WHERE id = %s", (record,))
    with migrated_postgres.connect("hrms_migrator") as owner, pytest.raises(errors.RaiseException):
        owner.execute("TRUNCATE access.user_roles")


def test_new_functions_are_not_executable_by_the_runtime_roles(superuser: psycopg.Connection) -> None:
    for function in ("identity.require_mfa_for_active_users()", "access.guard_role_assignments()"):
        for role in ("hrms_app", "hrms_worker"):
            row = superuser.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, function)
            ).fetchone()
            assert row == (False,), (function, role)
