"""The cluster bootstrap, infra/db/bootstrap-roles.sql (docs/database-design.md §3), run by a
superuser (local Compose, CI) and by a managed database's administrator: CREATEROLE and
CREATEDB but not superuser, as on Render. Both must end in the same state."""

import secrets
from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from testcontainers.core.container import DockerContainer

from tests.conftest import (
    DATABASE_NAME,
    INFRA_DIR,
    POSTGRES_IMAGE,
    ROLE_NAMES,
    PostgresServer,
    _wait_for_postgres,
    alembic_config,
    migration_settings,
)

RUNTIME_ROLES = ("hrms_app", "hrms_worker", "hrms_audit_retention")
MANAGED_ADMIN = "managed_admin"

# Owner privileges for the migrator, CONNECT for the runtime roles, nothing for PUBLIC.
EXPECTED_DATABASE_ACL = {
    ("hrms_migrator", "CONNECT"),
    ("hrms_migrator", "CREATE"),
    ("hrms_migrator", "TEMPORARY"),
    *((role, "CONNECT") for role in RUNTIME_ROLES),
}


def database_acl(connection: psycopg.Connection) -> set[tuple[str, str]]:
    rows = connection.execute(
        "SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, a.privilege_type "
        "FROM pg_database d, aclexplode(d.datacl) a WHERE d.datname = %s",
        (DATABASE_NAME,),
    ).fetchall()
    return {(row[0], row[1]) for row in rows}


def memberships(connection: psycopg.Connection, member: str) -> list[tuple[str, bool, bool, bool]]:
    """(role, admin, inherit, set) for every grant of an HRMS role to `member`."""
    return connection.execute(
        "SELECT r.rolname, m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member "
        "WHERE u.rolname = %s AND r.rolname = ANY(%s) ORDER BY 1, 2, 3, 4",
        (member, list(ROLE_NAMES)),
    ).fetchall()


def assert_bootstrap_result(superuser: psycopg.Connection, administrator: str) -> None:
    owner = superuser.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (DATABASE_NAME,)
    ).fetchone()
    assert owner == ("hrms_migrator",)
    assert database_acl(superuser) == EXPECTED_DATABASE_ACL
    # ADMIN OPTION only: the migrator manages the runtime roles' settings, never acts as them.
    assert memberships(superuser, "hrms_migrator") == [
        (role, True, False, False) for role in sorted(RUNTIME_ROLES)
    ]
    # The temporary SET on hrms_migrator is gone; nothing lets the administrator act as an HRMS role.
    assert not [grant for grant in memberships(superuser, administrator) if grant[2] or grant[3]]


def test_bootstrap_by_a_superuser(postgres: PostgresServer) -> None:
    with postgres.connect("postgres", "postgres") as superuser:
        assert_bootstrap_result(superuser, "postgres")
        assert memberships(superuser, "postgres") == []


@pytest.fixture(scope="module")
def managed_postgres() -> Iterator[PostgresServer]:
    """A server bootstrapped by an administrator with Render's privileges, not by a superuser."""
    superuser_password = secrets.token_urlsafe(24)
    role_passwords = {role: secrets.token_urlsafe(24) for role in ROLE_NAMES}
    container = (
        DockerContainer(POSTGRES_IMAGE)
        .with_env("POSTGRES_PASSWORD", superuser_password)
        .with_exposed_ports(5432)
        .with_volume_mapping(str(INFRA_DIR / "db"), "/hrms-bootstrap", "ro")
    )
    with container:
        _wait_for_postgres(container)
        setup = container.exec(
            [
                "psql", "--username", "postgres", "--dbname", "postgres", "-v", "ON_ERROR_STOP=1",
                "-c", f"CREATE ROLE {MANAGED_ADMIN} LOGIN CREATEROLE CREATEDB",
                "-c", f"CREATE DATABASE managed_admin_db OWNER {MANAGED_ADMIN}",
            ]
        )  # fmt: skip
        assert setup.exit_code == 0, setup.output.decode()
        result = container.exec(
            [
                "psql", "--username", MANAGED_ADMIN, "--dbname", "managed_admin_db",
                "-v", f"db_name={DATABASE_NAME}",
                "-v", f"migrator_password={role_passwords['hrms_migrator']}",
                "-v", f"app_password={role_passwords['hrms_app']}",
                "-v", f"worker_password={role_passwords['hrms_worker']}",
                "-v", f"audit_retention_password={role_passwords['hrms_audit_retention']}",
                "-f", "/hrms-bootstrap/bootstrap-roles.sql",
            ]
        )  # fmt: skip
        assert result.exit_code == 0, result.output.decode()
        yield PostgresServer(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            superuser_password=superuser_password,
            role_passwords=role_passwords,
        )


def test_bootstrap_by_a_managed_administrator(managed_postgres: PostgresServer) -> None:
    with managed_postgres.connect("postgres", "postgres") as superuser:
        is_superuser = superuser.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = %s", (MANAGED_ADMIN,)
        ).fetchone()
        assert is_superuser == (False,)
        assert_bootstrap_result(superuser, MANAGED_ADMIN)
        # What CREATEROLE gives the creator of a role (createrole_self_grant is empty): ADMIN only.
        assert memberships(superuser, MANAGED_ADMIN) == [
            (role, True, False, False) for role in sorted(ROLE_NAMES)
        ]


def test_migrations_and_grants_after_a_managed_bootstrap(managed_postgres: PostgresServer) -> None:
    command.upgrade(alembic_config(migration_settings(managed_postgres.url("hrms_migrator"))), "head")
    with managed_postgres.connect("hrms_migrator") as migrator:
        assert migrator.execute("SELECT version_num FROM alembic_version").fetchall() == [("0027",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            migrator.execute("SET ROLE hrms_app")
    for role in RUNTIME_ROLES:
        with managed_postgres.connect(role) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("CREATE TEMPORARY TABLE scratch (id int)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("UPDATE audit.audit_log SET id = id WHERE false")
