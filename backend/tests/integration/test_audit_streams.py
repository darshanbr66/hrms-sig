"""Audit stream tables: partitions, grants, append-only triggers (revision 0005).

docs/security-architecture.md §8 guarantees 1 and 3, and threat T18 (audit tampering):
application roles can only INSERT and SELECT; UPDATE, DELETE and TRUNCATE are refused even
for the owner; rows cannot be placed in another month's partition.
"""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import errors

from tests.conftest import PostgresServer, fresh_migrated_database

STREAMS = ("audit_log", "security_events")
RUNTIME_ROLES = ("hrms_app", "hrms_worker")
MINIMAL_ROW = {
    "audit_log": (
        "(occurred_at, actor_type, action, outcome) VALUES (now(), 'system', 'test.probe', 'success')"
    ),
    "security_events": "(occurred_at, event_type, severity) VALUES (now(), 'test.probe', 'info')",
}


def month_keys(start: datetime, count: int) -> list[str]:
    keys = []
    year, month = start.year, start.month
    for _ in range(count):
        keys.append(f"{year:04d}_{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def partitions(connection: psycopg.Connection, stream: str) -> set[str]:
    rows = connection.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = %s::regclass",
        (f"audit.{stream}",),
    ).fetchall()
    return {row[0] for row in rows}


def database_now(connection: psycopg.Connection) -> datetime:
    row = connection.execute("SELECT now()").fetchone()
    assert row is not None
    return row[0].astimezone(UTC)  # type: ignore[no-any-return]


@pytest.fixture
def superuser(migrated_postgres: PostgresServer) -> Iterator[psycopg.Connection]:
    with migrated_postgres.connect("postgres") as connection:
        yield connection


@pytest.mark.parametrize("stream", STREAMS)
def test_current_month_and_three_ahead_exist(superuser: psycopg.Connection, stream: str) -> None:
    expected = {f"{stream}_p{key}" for key in month_keys(database_now(superuser), 4)}
    assert expected <= partitions(superuser, stream)


@pytest.mark.parametrize("stream", STREAMS)
def test_privileges(superuser: psycopg.Connection, stream: str) -> None:
    def has(role: str, table: str, privilege: str) -> bool:
        row = superuser.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)).fetchone()
        assert row is not None
        return bool(row[0])

    parent = f"audit.{stream}"
    current_partition = f"audit.{stream}_p{month_keys(database_now(superuser), 1)[0]}"
    for role in RUNTIME_ROLES:
        assert has(role, parent, "INSERT")
        assert has(role, parent, "SELECT")
        for privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not has(role, parent, privilege), (role, privilege)
        # Rows reach a partition only through the parent, so its routing and triggers apply.
        assert not has(role, current_partition, "INSERT")
        assert has(role, current_partition, "SELECT")
    assert has("hrms_audit_retention", parent, "SELECT")
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not has("hrms_audit_retention", parent, privilege)


@pytest.mark.parametrize("role", RUNTIME_ROLES)
@pytest.mark.parametrize("stream", STREAMS)
def test_runtime_roles_insert_but_cannot_change_or_remove(
    migrated_postgres: PostgresServer, role: str, stream: str
) -> None:
    with migrated_postgres.connect(role) as connection:
        connection.execute(f"INSERT INTO audit.{stream} {MINIMAL_ROW[stream]}")
        for statement in (
            f"UPDATE audit.{stream} SET occurred_at = now()",
            f"DELETE FROM audit.{stream}",
            f"TRUNCATE audit.{stream}",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(statement)


@pytest.mark.parametrize("stream", STREAMS)
def test_triggers_refuse_changes_even_for_the_owner(migrated_postgres: PostgresServer, stream: str) -> None:
    with migrated_postgres.connect("hrms_migrator") as connection:
        connection.execute(f"INSERT INTO audit.{stream} {MINIMAL_ROW[stream]}")
        current_partition = f"audit.{stream}_p{month_keys(database_now(connection), 1)[0]}"
        for statement in (
            f"UPDATE audit.{stream} SET occurred_at = now()",
            f"DELETE FROM audit.{stream}",
            f"TRUNCATE audit.{stream}",
            f"UPDATE {current_partition} SET occurred_at = now()",
            f"DELETE FROM {current_partition}",
            f"TRUNCATE {current_partition}",
        ):
            with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
                connection.execute(statement)


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize(
    "recorded_at",
    [
        # Each lands in a partition that exists, so only the trigger can refuse it.
        "now() - interval '1 microsecond'",
        "now() + interval '1 second'",
        "now() + interval '40 days'",
        "clock_timestamp()",
    ],
)
def test_rows_cannot_be_back_or_forward_dated(
    migrated_postgres: PostgresServer, stream: str, recorded_at: str
) -> None:
    columns, values = MINIMAL_ROW[stream].split(" VALUES ")
    statement = (
        f"INSERT INTO audit.{stream} ({columns.strip('()')}, recorded_at) "
        f"VALUES ({values.strip('()')}, {recorded_at})"
    )
    # Inside a transaction, so clock_timestamp() is later than now().
    with migrated_postgres.connect("hrms_app") as connection, connection.transaction():
        connection.execute("SELECT pg_sleep(0.01)")
        with pytest.raises(errors.CheckViolation, match="set by the database"):
            connection.execute(statement)


@pytest.mark.parametrize("stream", STREAMS)
def test_the_database_assigns_record_ids(migrated_postgres: PostgresServer, stream: str) -> None:
    """A supplied ID is replaced, so IDs stay unique across partitions (the sealer keys
    chain links by record ID) and no row can claim another row's ID."""
    columns, values = MINIMAL_ROW[stream].split(" VALUES ")
    chosen = "01900000-0000-7000-8000-000000000001"
    statement = (
        f"INSERT INTO audit.{stream} ({columns.strip('()')}, id) "
        f"VALUES ({values.strip('()')}, '{chosen}') RETURNING id"
    )
    with migrated_postgres.connect("hrms_app") as connection:
        first = connection.execute(statement).fetchone()
        second = connection.execute(statement).fetchone()
    assert first is not None
    assert second is not None
    assert str(first[0]) != chosen
    assert str(second[0]) != chosen
    assert first[0] != second[0]
    assert first[0].version == second[0].version == 7


@pytest.mark.parametrize("permission", ["user.invite", "leave.request.approve.team"])
def test_audit_log_accepts_catalog_permission_keys(
    migrated_postgres: PostgresServer, permission: str
) -> None:
    """Regression: revision 0005 refused two-part keys such as `user.invite` (fixed in 0006)."""
    with migrated_postgres.connect("hrms_app") as connection:
        connection.execute(
            "INSERT INTO audit.audit_log (occurred_at, actor_type, action, outcome, permission_used) "
            "VALUES (now(), 'system', 'test.probe', 'success', %s)",
            (permission,),
        )


@pytest.mark.parametrize(
    "row",
    [
        "(occurred_at, actor_type, action, outcome) VALUES (now(), 'system', 'NotDotted', 'success')",
        "(occurred_at, actor_type, action, outcome) VALUES (now(), 'user', 'test.probe', 'success')",
        "(occurred_at, actor_type, actor_user_id, action, outcome) "
        "VALUES (now(), 'system', uuidv7(), 'test.probe', 'success')",
        "(occurred_at, actor_type, action, outcome) VALUES (now(), 'system', 'test.probe', 'maybe')",
        "(occurred_at, actor_type, action, outcome, changes) "
        "VALUES (now(), 'system', 'test.probe', 'success', '[1]')",
        "(occurred_at, actor_type, action, outcome, target_type) "
        "VALUES (now(), 'system', 'test.probe', 'success', 'employee')",
        "(occurred_at, actor_type, action, outcome, permission_used) "
        "VALUES (now(), 'system', 'test.probe', 'success', 'not-a-permission')",
        "(occurred_at, actor_type, action, outcome, permission_used) "
        "VALUES (now(), 'system', 'test.probe', 'success', 'leave')",
        "(occurred_at, actor_type, session_id, action, outcome) "
        "VALUES (now(), 'system', uuidv7(), 'test.probe', 'success')",
    ],
)
def test_audit_log_checks(migrated_postgres: PostgresServer, row: str) -> None:
    with migrated_postgres.connect("hrms_app") as connection, pytest.raises(errors.CheckViolation):
        connection.execute(f"INSERT INTO audit.audit_log {row}")


@pytest.mark.parametrize(
    "row",
    [
        "(occurred_at, event_type, severity) VALUES (now(), 'login.failed', 'critical')",
        "(occurred_at, event_type, severity, email_attempted_hash) "
        "VALUES (now(), 'login.failed', 'info', '\\x00')",
        "(occurred_at, event_type, severity, details) VALUES (now(), 'login.failed', 'info', '\"text\"')",
        "(occurred_at, event_type, severity, user_agent) "
        "VALUES (now(), 'login.failed', 'info', repeat('x', 513))",
    ],
)
def test_security_event_checks(migrated_postgres: PostgresServer, row: str) -> None:
    with migrated_postgres.connect("hrms_app") as connection, pytest.raises(errors.CheckViolation):
        connection.execute(f"INSERT INTO audit.security_events {row}")


def test_only_the_worker_may_create_partitions(migrated_postgres: PostgresServer) -> None:
    for role in ("hrms_app", "hrms_audit_retention"):
        with migrated_postgres.connect(role) as connection, pytest.raises(errors.InsufficientPrivilege):
            connection.execute("SELECT audit.ensure_partitions(3)")
    with migrated_postgres.connect("hrms_worker") as connection:
        connection.execute("SELECT audit.ensure_partitions(3)")


@pytest.mark.parametrize("months_ahead", [-1, 13])
def test_partition_horizon_is_bounded(migrated_postgres: PostgresServer, months_ahead: int) -> None:
    with (
        migrated_postgres.connect("hrms_worker") as connection,
        pytest.raises(errors.RaiseException, match="between 0 and 12"),
    ):
        connection.execute("SELECT audit.ensure_partitions(%s)", (months_ahead,))


def test_ensure_partitions_is_idempotent_and_safe_to_run_concurrently(postgres: PostgresServer) -> None:
    database = fresh_migrated_database(postgres)
    results: list[int] = []
    failures: list[BaseException] = []
    start = threading.Barrier(4)

    def call() -> None:
        try:
            with postgres.connect("hrms_worker", database) as connection:
                start.wait()
                row = connection.execute("SELECT audit.ensure_partitions(6)").fetchone()
                assert row is not None
                results.append(row[0])
        except BaseException as exc:  # collected and asserted on below
            failures.append(exc)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    # The migration created months 0..3; months 4..6 are new for each of the two streams.
    assert sorted(results) == [0, 0, 0, 6]
    with postgres.connect("postgres", database) as connection:
        now = database_now(connection)
        for stream in STREAMS:
            assert partitions(connection, stream) == {f"{stream}_p{key}" for key in month_keys(now, 7)}
            # Every partition, old or new, refuses TRUNCATE.
            for name in partitions(connection, stream):
                row = connection.execute(
                    "SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname = %s",
                    (f"audit.{name}", f"{name}_no_truncate"),
                ).fetchone()
                assert row == (1,), name
                # Partitions the worker created are owned and granted like the migration's.
                privileges = connection.execute(
                    "SELECT pg_get_userbyid(relowner), has_table_privilege('hrms_app', oid, 'SELECT'), "
                    "has_table_privilege('hrms_app', oid, 'INSERT') FROM pg_class WHERE oid = %s::regclass",
                    (f"audit.{name}",),
                ).fetchone()
                assert privileges == ("hrms_migrator", True, False), name
    with postgres.connect("hrms_worker", database) as connection:
        assert connection.execute("SELECT audit.ensure_partitions(6)").fetchone() == (0,)


def test_audited_insert_fails_when_its_partition_is_missing(postgres: PostgresServer) -> None:
    """No partition, no insert: the audited change fails rather than going unrecorded. The
    maintenance function then fails loudly instead of reporting the month as covered."""
    database = fresh_migrated_database(postgres)
    with postgres.connect("hrms_migrator", database) as connection:
        key = month_keys(database_now(connection), 1)[0]
        connection.execute(f"ALTER TABLE audit.audit_log DETACH PARTITION audit.audit_log_p{key}")
    with postgres.connect("hrms_app", database) as connection, pytest.raises(errors.CheckViolation):
        connection.execute(f"INSERT INTO audit.audit_log {MINIMAL_ROW['audit_log']}")
    with (
        postgres.connect("hrms_worker", database) as connection,
        pytest.raises(errors.RaiseException, match=r"is not a partition of audit\.audit_log"),
    ):
        connection.execute("SELECT audit.ensure_partitions(3)")
