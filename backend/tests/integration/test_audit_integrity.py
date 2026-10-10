"""Audit integrity: sealing, checkpoints, anchors, verification, retention and the tamper suite
(docs/security-architecture.md §8.1, threat model T18, M1 exit criterion "audit tamper tests").

Every test runs on a database of its own with a fresh write-once bucket on the RustFS
emulator. Rows are written by the API's role through the audit writer. Tampering is done as
the PostgreSQL superuser with triggers bypassed (`session_replication_role = replica`), the
strongest attacker the design addresses: someone who can rewrite any table but holds neither
the signing key nor delete rights on the write-once bucket. Past months are attached as
partitions by the migrator and filled by the superuser, with the jobs' clock set in the past.
"""

import asyncio
import ipaddress
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors
from pydantic import SecretStr

from app import cli
from app.platform.audit import canonical, chain
from app.platform.audit.canonical import CheckpointKind, Stream
from app.platform.audit.checkpoints import UNSEALED_ALERT
from app.platform.audit.integrity import AuditIntegrity
from app.platform.audit.records import (
    AuditActor,
    AuditEvent,
    RequestContext,
    SecurityEvent,
    SecurityEventType,
    Severity,
    ValueChange,
)
from app.platform.audit.retention import RetentionResult, apply_retention
from app.platform.audit.verify import ANCHOR_GRACE, Report, Scope, verify_stream
from app.platform.audit.writer import AuditWriter
from app.platform.config import AppEnv, AuditVerifySettings
from app.platform.db import Database, create_engine
from app.platform.storage import AnchorState, AnchorStore, AnchorStoreError
from tests.api_support import FakeClock, audit_signer, audit_verifier, audit_verify_keys_json
from tests.conftest import PostgresServer, S3Server, fresh_migrated_database

KEY_FIELDS = ("audit_log", "security_events")


class FailingAnchors:
    """An anchor store that is down: every call fails, as when the bucket is unreachable."""

    async def put(self, key: str, body: bytes) -> None:
        raise AnchorStoreError("anchor storage is unavailable: ConnectionError")

    async def state(self, key: str) -> AnchorState:
        raise AnchorStoreError("anchor storage is unavailable: ConnectionError")

    async def keys(self, prefix: str, *, start_after: str | None = None) -> list[str]:
        raise AnchorStoreError("anchor storage is unavailable: ConnectionError")


@dataclass
class Env:
    postgres: PostgresServer
    name: str
    app: Database
    worker: Database
    retention: Database
    anchors: AnchorStore
    bucket: str
    clock: FakeClock
    mismatches: list[Report] = field(default_factory=list)

    def integrity(self, *, anchors: AnchorStore | None = None, months: int | None = None) -> AuditIntegrity:
        async def on_mismatch(session: object, report: Report, now: datetime) -> None:
            self.mismatches.append(report)

        return AuditIntegrity(
            database=self.worker,
            retention_database=self.retention,
            anchors=anchors or self.anchors,
            signer=audit_signer(),
            verifier=audit_verifier(),
            clock=self.clock,
            audit=AuditWriter(self.clock),
            retention_months={Stream.AUDIT_LOG: months, Stream.SECURITY_EVENTS: months},
            on_mismatch=on_mismatch,
        )

    def connect(self, role: str = "postgres") -> psycopg.Connection:
        return self.postgres.connect(role, self.name)

    def tamper(self, statement: str, *params: object) -> None:
        """As the superuser, with every trigger bypassed."""
        with self.connect() as connection:
            connection.execute("SET session_replication_role = replica")
            connection.execute(statement, params)

    def fetch(self, statement: str, *params: object) -> list[tuple[object, ...]]:
        with self.connect() as connection:
            return connection.execute(statement, params).fetchall()

    async def write(self, count: int = 5) -> None:
        """Rows of both streams through the audit writer, as the API writes them."""
        writer = AuditWriter(self.clock)
        context = RequestContext(
            request_id=uuid.uuid4(), ip=ipaddress.ip_address("203.0.113.7"), user_agent="Test browser/1.0"
        )
        async with self.app.unit_of_work() as session:
            for index in range(count):
                await writer.record(
                    session,
                    AuditEvent(
                        action="test.integrity.changed",
                        actor=AuditActor.user(uuid.uuid4()),
                        context=context,
                        target_type="setting",
                        target_id=f"test.value.{index}",
                        changes={"value": ValueChange(index, index + 1)},
                        reason="Test - integrity",
                    ),
                )
                await writer.record_security_event(
                    session,
                    SecurityEvent(
                        SecurityEventType.LOGIN_FAILED,
                        Severity.WARNING,
                        context=context,
                        email_attempted_hash=bytes([index]) * 32,
                        details={"reason": "password", "attempt": index},
                    ),
                )

    async def verify(self, stream: Stream = Stream.AUDIT_LOG, scope: Scope = Scope.FULL) -> Report:
        return await verify_stream(
            self.worker, self.anchors, audit_verifier(), stream=stream, scope=scope, now=self.clock.now()
        )

    def add_months(self, *keys: str) -> None:
        """Attach past month partitions (as the owner), then let ensure_partitions add their
        chain partitions."""
        with self.connect("hrms_migrator") as connection:
            for key in keys:
                start, end = canonical.month_start(key), canonical.month_end(key)
                for stream in KEY_FIELDS:
                    name = f"{stream}_p{key.replace('-', '_')}"
                    connection.execute(
                        f"CREATE TABLE audit.{name} "
                        f"(LIKE audit.{stream} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"
                    )
                    connection.execute(
                        f"ALTER TABLE audit.{stream} ATTACH PARTITION audit.{name} "
                        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                    )
        with self.connect("hrms_worker") as connection:
            connection.execute("SELECT audit.ensure_partitions(0)")

    def add_past_rows(self, at: datetime, count: int = 3, stream: str = "audit_log") -> None:
        if stream == "audit_log":
            statement = (
                "INSERT INTO audit.audit_log (id, recorded_at, occurred_at, actor_type, action, outcome) "
                "SELECT uuidv7(), %s::timestamptz + g * interval '1 second', %s, 'system', "
                "'test.past.event', 'success' FROM generate_series(0, %s - 1) g"
            )
        else:
            statement = (
                "INSERT INTO audit.security_events (id, recorded_at, occurred_at, event_type, severity) "
                "SELECT uuidv7(), %s::timestamptz + g * interval '1 second', %s, 'login.failed', 'info' "
                "FROM generate_series(0, %s - 1) g"
            )
        self.tamper(statement, at, at, count)


def checks(report: Report) -> set[str]:
    return {finding.check for finding in report.findings}


@pytest.fixture
async def env(postgres: PostgresServer, s3: S3Server) -> AsyncIterator[Env]:
    name = fresh_migrated_database(postgres)
    app, worker, retention = (
        Database(create_engine(postgres.url(role, name), application_name=f"test-{role}"))
        for role in ("hrms_app", "hrms_worker", "hrms_audit_retention")
    )
    databases = (app, worker, retention)
    bucket = s3.create_anchor_bucket()
    try:
        yield Env(
            postgres,
            name,
            app,
            worker,
            retention,
            anchors=s3.anchor_store(bucket),
            bucket=bucket,
            clock=FakeClock(),
        )
    finally:
        for database in databases:
            await database.dispose()


@pytest.fixture
def superuser(env: Env) -> Iterator[psycopg.Connection]:
    with env.connect() as connection:
        yield connection


# --- sealing, checkpoints, anchors --------------------------------------------------------------


async def test_rows_are_sealed_checkpointed_signed_and_anchored(env: Env) -> None:
    await env.write(5)
    integrity = env.integrity()
    assert await integrity.seal() == 10
    assert await integrity.seal() == 0
    assert await integrity.checkpoint() >= 2

    key = canonical.month_key(env.clock.now())
    for stream in Stream:
        links = env.fetch(
            "SELECT position FROM audit.chain_links WHERE stream = %s AND partition_key = %s "
            "ORDER BY position",
            stream.value,
            key,
        )
        assert [row[0] for row in links] == [1, 2, 3, 4, 5]
        async with env.worker.unit_of_work() as session:
            [checkpoint] = [
                item for item in await chain.checkpoints(session, stream) if item.fields.partition_key == key
            ]
        assert checkpoint.fields.kind is CheckpointKind.DAILY
        assert checkpoint.fields.last_position == 5
        assert audit_verifier().verify("test-key", checkpoint.checkpoint_hash, checkpoint.signature)
        state = await env.anchors.state(checkpoint.anchor_object_key)
        assert [version.body for version in state.versions] == [checkpoint.body]
        assert state.versions[0].retention_mode == "COMPLIANCE"
        for scope in Scope:
            report = await env.verify(stream, scope)
            assert report.ok, report.findings
            assert report.rows_checked == 5
    # One daily checkpoint per partition per day, and none when nothing changed.
    assert await integrity.checkpoint() == 0
    await env.write(1)
    await integrity.seal()
    assert await integrity.checkpoint() == 0
    env.clock.advance(timedelta(days=1))
    assert await integrity.checkpoint() == 2
    assert (await env.verify()).ok


async def test_concurrent_sealers_build_one_chain(env: Env) -> None:
    await env.write(40)
    integrity = env.integrity()
    sealed = await asyncio.gather(*(integrity.seal() for _ in range(4)))
    assert sum(sealed) == 80
    positions = env.fetch(
        "SELECT stream, count(*), min(position), max(position), count(DISTINCT record_id) "
        "FROM audit.chain_links GROUP BY stream"
    )
    assert sorted(positions) == [("audit_log", 40, 1, 40, 40), ("security_events", 40, 1, 40, 40)]
    await integrity.checkpoint()
    assert (await env.verify()).ok


async def test_chain_tables_are_insert_only_and_written_only_by_the_jobs(
    env: Env, superuser: psycopg.Connection
) -> None:
    await env.write(1)
    integrity = env.integrity()
    await integrity.seal()
    await integrity.checkpoint()

    def has(role: str, table: str, privilege: str) -> bool:
        row = superuser.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)).fetchone()
        assert row is not None
        return bool(row[0])

    assert not has("hrms_app", "audit.chain_links", "INSERT")
    assert not has("hrms_app", "audit.chain_checkpoints", "INSERT")
    assert has("hrms_worker", "audit.chain_links", "INSERT")
    assert has("hrms_worker", "audit.chain_checkpoints", "INSERT")
    assert not has("hrms_audit_retention", "audit.chain_links", "INSERT")
    assert has("hrms_audit_retention", "audit.chain_checkpoints", "INSERT")
    for role in ("hrms_app", "hrms_worker", "hrms_audit_retention"):
        assert has(role, "audit.chain_links", "SELECT")
        for table in ("audit.chain_links", "audit.chain_checkpoints"):
            for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                assert not has(role, table, privilege), (role, table, privilege)
    # The append-only triggers refuse even the owner.
    with env.connect("hrms_migrator") as owner:
        for statement in (
            "UPDATE audit.chain_links SET position = position",
            "DELETE FROM audit.chain_checkpoints",
            "TRUNCATE audit.chain_checkpoints",
            "TRUNCATE audit.chain_links",
        ):
            with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
                owner.execute(statement)
    # A checkpoint must continue its stream's chain: no gaps, no forks.
    with env.connect("hrms_worker") as worker, pytest.raises(errors.CheckViolation, match="must follow"):
        worker.execute(
            "INSERT INTO audit.chain_checkpoints (stream, checkpoint_no, kind, partition_key, last_position, "
            "row_count, last_link_hash, prev_checkpoint_hash, checkpoint_hash, signature, key_id, "
            "anchor_object_key, created_at) VALUES ('audit_log', 9, 'daily', '2026-01', 0, 0, "
            "decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'), "
            "decode(repeat('00', 32), 'hex'), decode(repeat('00', 64), 'hex'), 'k', "
            "'audit_log/000000000009.json', now())"
        )


async def test_every_audit_partition_has_its_chain_partition(env: Env) -> None:
    env.add_months("2025-01")
    names = {
        row[0]
        for row in env.fetch(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'audit.chain_links_audit_log'::regclass"
        )
    }
    current = canonical.month_key(env.clock.now()).replace("-", "_")
    assert {"chain_links_audit_log_p2025_01", f"chain_links_audit_log_p{current}"} <= names


# --- partition boundaries and partition-final checkpoints ------------------------------------


async def test_months_close_with_a_final_checkpoint_and_later_rows_are_tampering(env: Env) -> None:
    env.add_months("2025-01", "2025-02")
    env.add_past_rows(datetime(2025, 1, 31, 23, 59, 58, tzinfo=UTC), 2)  # the last second of January
    env.add_past_rows(datetime(2025, 2, 1, tzinfo=UTC), 1)  # the first instant of February
    integrity = env.integrity()

    # Within the margin after the month ends, January is still open: a daily checkpoint only.
    env.clock.current = datetime(2025, 2, 1, 0, 30, tzinfo=UTC)
    await integrity.seal()
    await integrity.checkpoint()
    links = env.fetch("SELECT partition_key, count(*) FROM audit.chain_links GROUP BY 1 ORDER BY 1")
    assert links == [("2025-01", 2), ("2025-02", 1)]
    kinds = env.fetch(
        "SELECT partition_key, kind FROM audit.chain_checkpoints WHERE stream = 'audit_log' ORDER BY 1"
    )
    assert kinds == [("2025-01", "daily"), ("2025-02", "daily")]

    # A row committed late, inside the margin, is still sealed before the month closes.
    env.add_past_rows(datetime(2025, 1, 31, 23, 59, 59, 999999, tzinfo=UTC), 1)
    env.clock.current = datetime(2025, 2, 1, 1, 0, 1, tzinfo=UTC)
    await integrity.checkpoint()
    [(final_position,)] = env.fetch(
        "SELECT last_position FROM audit.chain_checkpoints "
        "WHERE kind = 'partition_final' AND stream = 'audit_log'"
    )
    assert final_position == 3
    report = await env.verify()
    assert report.ok, report.findings

    # After the final checkpoint, nothing more is sealed there: a new row is tampering.
    env.add_past_rows(datetime(2025, 1, 20, tzinfo=UTC), 1)
    await integrity.seal()
    assert env.fetch(
        "SELECT count(*) FROM audit.chain_links WHERE partition_key = '2025-01' AND stream = 'audit_log'"
    ) == [(3,)]
    report = await env.verify()
    assert checks(report) == {"rows_after_final"}
    # A month that closed long ago without a final checkpoint means the job stopped.
    env.clock.current = datetime(2025, 3, 3, tzinfo=UTC)
    assert "partition_not_final" in checks(await env.verify())


async def test_rows_left_unsealed_are_reported(env: Env) -> None:
    await env.write(2)
    env.clock.advance(UNSEALED_ALERT + timedelta(minutes=1))
    report = await env.verify(scope=Scope.RECENT)
    assert checks(report) == {"unsealed_rows"}
    await env.integrity().seal()
    assert (await env.verify(scope=Scope.RECENT)).ok


# --- tamper suite ---------------------------------------------------------------------------------


async def sealed(env: Env, count: int = 5) -> AuditIntegrity:
    await env.write(count)
    integrity = env.integrity()
    await integrity.seal()
    await integrity.checkpoint()
    assert (await env.verify()).ok
    return integrity


def record_at(env: Env, position: int) -> object:
    [(record_id,)] = env.fetch(
        "SELECT record_id FROM audit.chain_links WHERE stream = 'audit_log' AND position = %s", position
    )
    return record_id


async def test_a_modified_row_is_detected(env: Env) -> None:
    await sealed(env)
    env.tamper("UPDATE audit.audit_log SET reason = 'nothing happened' WHERE id = %s", record_at(env, 3))
    report = await env.verify()
    assert checks(report) == {"row_modified"}
    assert [finding.detail for finding in report.findings] == ["position 3"]


async def test_a_modified_security_event_is_detected(env: Env) -> None:
    await sealed(env)
    env.tamper(
        "UPDATE audit.security_events SET severity = 'info' WHERE email_attempted_hash = %s", bytes([2]) * 32
    )
    assert checks(await env.verify(Stream.SECURITY_EVENTS)) == {"row_modified"}


async def test_a_deleted_row_is_detected(env: Env) -> None:
    await sealed(env)
    env.tamper("DELETE FROM audit.audit_log WHERE id = %s", record_at(env, 2))
    assert checks(await env.verify()) == {"row_missing"}


async def test_an_inserted_row_with_a_sealed_rows_id_is_detected(env: Env) -> None:
    await sealed(env)
    env.tamper(
        "INSERT INTO audit.audit_log SELECT id, recorded_at + interval '1 microsecond', occurred_at, "
        "actor_type, "
        "actor_user_id, session_id, grant_request_id, request_id, ip, user_agent, action, permission_used, "
        "target_type, target_id, subject_employee_id, outcome, changes, 'forged' "
        "FROM audit.audit_log WHERE id = %s",
        record_at(env, 4),
    )
    assert "duplicate_record" in checks(await env.verify())


async def test_reordered_rows_are_detected(env: Env) -> None:
    await sealed(env)
    second, third = record_at(env, 2), record_at(env, 3)
    swap = "UPDATE audit.chain_links SET record_id = %s WHERE stream = 'audit_log' AND position = %s"
    env.tamper(swap, uuid.uuid4(), 2)
    env.tamper(swap, second, 3)
    env.tamper(swap, third, 2)
    report = await env.verify()
    assert checks(report) == {"row_modified"}
    assert sorted(finding.detail for finding in report.findings) == ["position 2", "position 3"]


async def test_a_truncated_tail_is_detected(env: Env) -> None:
    await sealed(env)
    env.tamper("DELETE FROM audit.chain_links WHERE stream = 'audit_log' AND position > 3")
    report = await env.verify()
    assert "checkpoint_beyond_chain" in checks(report)


async def test_a_removed_link_is_detected(env: Env) -> None:
    await sealed(env)
    env.tamper("DELETE FROM audit.chain_links WHERE stream = 'audit_log' AND position = 2")
    assert {"link_gap", "link_broken"} <= checks(await env.verify())


async def test_a_chain_rewritten_without_the_signing_key_is_detected(env: Env) -> None:
    """The attacker edits a row, then recomputes every hash the database holds: the row's
    digest, every later link and the checkpoint. Only the signature and the write-once copy
    cannot follow."""
    await sealed(env)
    key = canonical.month_key(env.clock.now())
    env.tamper("UPDATE audit.audit_log SET reason = 'rewritten' WHERE id = %s", record_at(env, 2))
    async with env.worker.unit_of_work() as session:
        await chain.use_row_text_settings(session)
        links = await chain.links_with_rows(session, Stream.AUDIT_LOG, key, after=0, limit=100)
        [checkpoint] = [
            item
            for item in await chain.checkpoints(session, Stream.AUDIT_LOG)
            if item.fields.partition_key == key
        ]
    previous = canonical.genesis(Stream.AUDIT_LOG, key)
    for link in links:
        assert link.row is not None
        digest = canonical.row_digest(Stream.AUDIT_LOG, link.row)
        previous = canonical.link_hash(previous, digest, link.position)
        env.tamper(
            "UPDATE audit.chain_links SET row_digest = %s, link_hash = %s "
            "WHERE stream = 'audit_log' AND position = %s",
            digest,
            previous,
            link.position,
        )
    forged = replace(checkpoint.fields, last_link_hash=previous)
    env.tamper(
        "UPDATE audit.chain_checkpoints SET last_link_hash = %s, checkpoint_hash = %s "
        "WHERE stream = 'audit_log' AND checkpoint_no = %s",
        previous,
        canonical.checkpoint_hash(forged),
        checkpoint.fields.checkpoint_no,
    )
    report = await env.verify()
    assert checks(report) == {"checkpoint_signature_invalid", "anchor_mismatch"}
    # Signing with a key the verifier does not trust fails the same way.
    stranger = audit_signer("test-key", b"x" * 32)
    env.tamper(
        "UPDATE audit.chain_checkpoints SET signature = %s WHERE stream = 'audit_log' AND checkpoint_no = %s",
        stranger.sign(canonical.checkpoint_hash(forged)),
        checkpoint.fields.checkpoint_no,
    )
    assert checks(await env.verify()) == {"checkpoint_signature_invalid", "anchor_mismatch"}


async def test_deleted_checkpoints_are_detected(env: Env) -> None:
    integrity = await sealed(env)
    await env.write(2)
    await integrity.seal()
    env.clock.advance(timedelta(days=1))
    await integrity.checkpoint()
    [(last,)] = env.fetch("SELECT max(checkpoint_no) FROM audit.chain_checkpoints WHERE stream = 'audit_log'")
    assert last == 2

    env.tamper("DELETE FROM audit.chain_checkpoints WHERE stream = 'audit_log' AND checkpoint_no = 2")
    # The tail looks complete in the database; its write-once copy does not.
    for scope in Scope:
        assert checks(await env.verify(scope=scope)) == {"anchor_without_checkpoint"}
    env.tamper("DELETE FROM audit.chain_checkpoints WHERE stream = 'audit_log' AND checkpoint_no = 1")
    assert "anchor_without_checkpoint" in checks(await env.verify())


async def test_a_deleted_checkpoint_in_the_middle_is_a_gap(env: Env) -> None:
    integrity = await sealed(env)
    for _ in range(2):
        await env.write(1)
        await integrity.seal()
        env.clock.advance(timedelta(days=1))
        await integrity.checkpoint()
    env.tamper("DELETE FROM audit.chain_checkpoints WHERE stream = 'audit_log' AND checkpoint_no = 2")
    assert {"checkpoint_gap", "checkpoint_chain_broken", "anchor_without_checkpoint"} <= checks(
        await env.verify()
    )


async def test_a_partition_dropped_outside_retention_is_detected(env: Env) -> None:
    env.add_months("2025-01")
    env.add_past_rows(datetime(2025, 1, 10, tzinfo=UTC), 2)
    env.clock.current = datetime(2025, 2, 2, tzinfo=UTC)
    integrity = env.integrity()
    await integrity.seal()
    await integrity.checkpoint()
    assert (await env.verify()).ok
    with env.connect("hrms_migrator") as owner:
        owner.execute("ALTER TABLE audit.audit_log DETACH PARTITION audit.audit_log_p2025_01")
        owner.execute("DROP TABLE audit.audit_log_p2025_01")
    assert checks(await env.verify()) == {"partition_missing"}


async def test_a_partition_dropped_before_its_first_checkpoint_is_detected(env: Env) -> None:
    env.add_months("2025-01")
    env.add_past_rows(datetime(2025, 1, 10, tzinfo=UTC), 2)
    env.clock.current = datetime(2025, 1, 10, 0, 5, tzinfo=UTC)
    await env.integrity().seal()
    with env.connect("hrms_migrator") as owner:
        owner.execute("ALTER TABLE audit.audit_log DETACH PARTITION audit.audit_log_p2025_01")
        owner.execute("DROP TABLE audit.audit_log_p2025_01")
    report = await env.verify()
    assert checks(report) == {"partition_missing"}
    assert [finding.partition_key for finding in report.findings] == ["2025-01"]


async def test_an_anchor_overwritten_with_a_new_version_is_detected(env: Env) -> None:
    await sealed(env)
    [(object_key,)] = env.fetch(
        "SELECT anchor_object_key FROM audit.chain_checkpoints WHERE stream = 'audit_log' LIMIT 1"
    )
    await env.anchors.put(str(object_key), b'{"forged": true}')
    assert checks(await env.verify()) == {"anchor_mismatch"}


# --- retention ----------------------------------------------------------------------------------


def months_ready_for_retention(env: Env) -> None:
    env.add_months("2025-01", "2025-02", "2025-03")
    for month in (1, 2, 3):
        env.add_past_rows(datetime(2025, month, 10, tzinfo=UTC), 2)
        env.add_past_rows(datetime(2025, month, 10, tzinfo=UTC), 2, stream="security_events")


async def test_retention_removes_a_partition_only_after_a_signed_tombstone(env: Env) -> None:
    months_ready_for_retention(env)
    integrity = env.integrity(months=1)
    env.clock.current = datetime(2025, 4, 15, tzinfo=UTC)
    await integrity.seal()
    await integrity.checkpoint()

    # Without its tombstone the database refuses to drop the month, whoever asks.
    with (
        env.connect("hrms_audit_retention") as retention,
        pytest.raises(errors.RaiseException, match="tombstone"),
    ):
        retention.execute("SELECT audit.drop_expired_partition('audit_log', '2025-01')")
    for role in ("hrms_app", "hrms_worker"):
        with env.connect(role) as other, pytest.raises(errors.InsufficientPrivilege):
            other.execute("SELECT audit.drop_expired_partition('audit_log', '2025-01')")
    # A month that can still receive rows is never removed, tombstone or not.
    with (
        env.connect("hrms_audit_retention") as retention,
        pytest.raises(errors.RaiseException, match="still receive"),
    ):
        retention.execute(
            "SELECT audit.drop_expired_partition('audit_log', %s)", (canonical.month_key(datetime.now(UTC)),)
        )

    results = await integrity.retention()
    assert [(result.stream, result.removed) for result in results] == [
        (Stream.AUDIT_LOG, ["2025-01", "2025-02"]),
        (Stream.SECURITY_EVENTS, ["2025-01", "2025-02"]),
    ]
    tombstones = env.fetch(
        "SELECT stream, partition_key, last_position, retention_policy_version, anchor_object_key "
        "FROM audit.chain_checkpoints WHERE kind = 'retention_tombstone' ORDER BY 1, 2"
    )
    assert [row[:4] for row in tombstones] == [
        ("audit_log", "2025-01", 2, "retain_months=1"),
        ("audit_log", "2025-02", 2, "retain_months=1"),
        ("security_events", "2025-01", 2, "retain_months=1"),
        ("security_events", "2025-02", 2, "retain_months=1"),
    ]
    for row in tombstones:
        assert (await env.anchors.state(str(row[4]))).versions
    remaining = {row[0] for row in env.fetch("SELECT relname FROM pg_class WHERE relname ~ '_p2025_0[1-3]$'")}
    assert remaining == {
        f"{prefix}{stream}_p2025_03" for prefix in ("", "chain_links_") for stream in KEY_FIELDS
    }
    # The removed months are accounted for; everything still verifies.
    for stream in Stream:
        report = await env.verify(stream)
        assert report.ok, report.findings
    assert await integrity.retention() == [
        RetentionResult(Stream.AUDIT_LOG),
        RetentionResult(Stream.SECURITY_EVENTS),
    ]
    assert env.mismatches == []


async def test_retention_stops_and_alerts_when_verification_fails(env: Env) -> None:
    months_ready_for_retention(env)
    integrity = env.integrity(months=1)
    env.clock.current = datetime(2025, 4, 15, tzinfo=UTC)
    await integrity.seal()
    await integrity.checkpoint()
    env.tamper("UPDATE audit.audit_log SET action = 'test.past.other' WHERE recorded_at < '2025-02-01'")

    results = await integrity.retention()
    assert results[0].removed == []
    assert results[0].failed_verification is not None
    assert checks(results[0].failed_verification) == {"row_modified"}
    assert env.fetch("SELECT to_regclass('audit.audit_log_p2025_01') IS NOT NULL") == [(True,)]
    assert env.fetch(
        "SELECT count(*) FROM audit.chain_checkpoints "
        "WHERE kind = 'retention_tombstone' AND stream = 'audit_log'"
    ) == [(0,)]
    assert [report.stream for report in env.mismatches] == [Stream.AUDIT_LOG]
    [(details,)] = env.fetch(
        "SELECT details FROM audit.security_events "
        "WHERE event_type = 'audit.chain_mismatch' AND severity = 'high'"
    )
    assert details == {"stream": "audit_log", "scope": "full", "findings": 2, "checks": "row_modified"}


async def test_retention_resumes_after_a_tombstone_was_written(env: Env) -> None:
    months_ready_for_retention(env)
    integrity = env.integrity(months=1)
    env.clock.current = datetime(2025, 4, 15, tzinfo=UTC)
    await integrity.seal()
    await integrity.checkpoint()
    # A run that stopped after the tombstone committed, before its upload and the drop.
    with pytest.raises(AnchorStoreError):
        await apply_retention(
            env.retention,
            _PutFails(env.anchors),
            audit_signer(),
            audit_verifier(),
            stream=Stream.AUDIT_LOG,
            months=1,
            now=env.clock.now(),
        )
    assert env.fetch("SELECT count(*) FROM audit.chain_checkpoints WHERE kind = 'retention_tombstone'") == [
        (1,)
    ]
    assert env.fetch("SELECT to_regclass('audit.audit_log_p2025_01') IS NOT NULL") == [(True,)]
    result = await apply_retention(
        env.retention,
        env.anchors,
        audit_signer(),
        audit_verifier(),
        stream=Stream.AUDIT_LOG,
        months=1,
        now=env.clock.now(),
    )
    assert result.removed == ["2025-01", "2025-02"]
    assert env.fetch("SELECT count(*) FROM audit.chain_checkpoints WHERE kind = 'retention_tombstone'") == [
        (2,)
    ]
    assert (await env.verify()).ok


class _PutFails:
    """Reads work, writes fail: the bucket accepted no new object."""

    def __init__(self, anchors: AnchorStore) -> None:
        self._anchors = anchors

    async def put(self, key: str, body: bytes) -> None:
        raise AnchorStoreError("anchor storage is unavailable: ConnectionError")

    async def state(self, key: str) -> AnchorState:
        return await self._anchors.state(key)

    async def keys(self, prefix: str, *, start_after: str | None = None) -> list[str]:
        return await self._anchors.keys(prefix, start_after=start_after)


# --- failure and recovery ---------------------------------------------------------------------


async def test_a_failed_anchor_upload_is_retried_later(env: Env) -> None:
    await env.write(3)
    await env.integrity().seal()
    with pytest.raises(AnchorStoreError):
        await env.integrity(anchors=_PutFails(env.anchors)).checkpoint()
    # The checkpoint committed without its copy: within the grace period that is not a finding.
    assert env.fetch("SELECT count(*) FROM audit.chain_checkpoints WHERE stream = 'audit_log'") == [(1,)]
    assert (await env.verify()).ok
    env.clock.advance(ANCHOR_GRACE + timedelta(minutes=1))
    assert checks(await env.verify()) == {"anchor_missing"}
    # The next run uploads what is missing.
    await env.integrity().checkpoint()
    assert (await env.verify()).ok


async def test_an_unreachable_bucket_is_a_finding(env: Env) -> None:
    await sealed(env)
    report = await verify_stream(
        env.worker,
        FailingAnchors(),
        audit_verifier(),
        stream=Stream.AUDIT_LOG,
        scope=Scope.FULL,
        now=env.clock.now(),
    )
    assert checks(report) == {"anchor_store_unavailable"}


async def test_verification_failures_are_reported_once_per_stream(env: Env) -> None:
    await sealed(env)
    env.tamper("UPDATE audit.audit_log SET reason = 'changed' WHERE id = %s", record_at(env, 1))
    reports = await env.integrity().verify(Scope.RECENT)
    assert [(report.stream, report.ok) for report in reports] == [
        (Stream.AUDIT_LOG, False),
        (Stream.SECURITY_EVENTS, True),
    ]
    assert [report.stream for report in env.mismatches] == [Stream.AUDIT_LOG]
    [(severity, details)] = env.fetch(
        "SELECT severity, details FROM audit.security_events WHERE event_type = 'audit.chain_mismatch'"
    )
    assert severity == "high"
    assert details == {"stream": "audit_log", "scope": "recent", "findings": 1, "checks": "row_modified"}


# --- operator command -----------------------------------------------------------------------------


async def test_the_audit_verify_command_prints_every_problem(
    env: Env, s3: S3Server, capsys: pytest.CaptureFixture[str]
) -> None:
    await sealed(env)
    await env.integrity().checkpoint()
    settings = AuditVerifySettings(
        app_env=AppEnv.TEST,
        database_url_worker=env.postgres.url("hrms_worker", env.name),
        audit_verify_public_keys=audit_verify_keys_json(),
        s3_endpoint_url=s3.endpoint_url,
        s3_region=s3.region,
        audit_anchor_bucket=env.bucket,
        audit_anchor_access_key_id=SecretStr(s3.access_key_id),
        audit_anchor_secret_access_key=SecretStr(s3.secret_access_key),
    )
    reports = await cli.audit_verify(settings, Scope.FULL)
    assert [(report.stream, report.ok) for report in reports] == [
        (Stream.AUDIT_LOG, True),
        (Stream.SECURITY_EVENTS, True),
    ]
    env.tamper("UPDATE audit.audit_log SET reason = 'changed' WHERE id = %s", record_at(env, 2))
    reports = await cli.audit_verify(settings, Scope.FULL)
    cli._print_reports(reports)
    output = capsys.readouterr().out
    key = canonical.month_key(env.clock.now())
    assert "audit_log: FAILED (full; " in output
    assert f"  row_modified [{key}]: position 2\n" in output
    assert "security_events: OK" in output
    # The report names positions and checks, never row contents.
    assert "changed" not in output
