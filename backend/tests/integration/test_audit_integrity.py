"""Audit integrity: sealing, checkpoints, the witness, verification, retention and the tamper
suite (docs/security-architecture.md §8.1, threat model T18, M1 exit criterion "audit tamper
tests").

Every test runs on a database of its own. Records are written by the API's database user through
the audit writer. Tampering is done as the MongoDB root user, the strongest attacker the design
addresses: someone who can rewrite any collection but holds neither the signing key nor the
mailboxes that keep the daily witness. Past months are written by root with the jobs' clock set
in the past.
"""

import asyncio
import ipaddress
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pymongo import MongoClient
from pymongo.database import Database as SyncDatabase
from pymongo.errors import OperationFailure

from app import cli
from app.platform.audit import canonical, chain, retention, witness
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
from app.platform.audit.retention import RetentionResult
from app.platform.audit.tables import AuditLogRecord, SecurityEventRecord
from app.platform.audit.verify import Report, Scope, verify_stream
from app.platform.audit.writer import AuditWriter
from app.platform.config import AppEnv, AuditVerifySettings
from app.platform.db import Database, create_client, to_document
from tests.api_support import (
    FakeClock,
    RecordingSender,
    audit_signer,
    audit_verifier,
    audit_verify_keys_json,
    create_organization,
)
from tests.conftest import MongoServer, fresh_migrated_database

LINKS = "audit_chain_links"
CHECKPOINTS = "audit_checkpoints"
RECORDS = {Stream.AUDIT_LOG: "audit_log", Stream.SECURITY_EVENTS: "security_events"}


@dataclass
class Env:
    mongo: MongoServer
    name: str
    organization: uuid.UUID
    app: Database
    worker: Database
    retention: Database
    root: MongoClient[dict[str, Any]]
    clock: FakeClock
    mismatches: list[Report] = field(default_factory=list)

    @property
    def db(self) -> SyncDatabase[dict[str, Any]]:
        """The database as root: past every role and every application rule."""
        return self.root[self.name]

    def integrity(self, *, months: int | None = None) -> AuditIntegrity:
        async def on_mismatch(report: Report, now: datetime) -> None:
            self.mismatches.append(report)

        return AuditIntegrity(
            database=self.worker,
            retention_database=self.retention,
            signer=audit_signer(),
            verifier=audit_verifier(),
            clock=self.clock,
            audit=AuditWriter(self.clock),
            retention_months={Stream.AUDIT_LOG: months, Stream.SECURITY_EVENTS: months},
            on_mismatch=on_mismatch,
        )

    async def write(self, count: int = 5) -> None:
        """Records of both streams through the audit writer, as the API writes them."""
        writer = AuditWriter(self.clock)
        context = RequestContext(
            request_id=uuid.uuid4(), ip=ipaddress.ip_address("203.0.113.7"), user_agent="Test browser/1.0"
        )
        async with self.app.unit_of_work(tenant=self.organization) as session:
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
            self.worker, audit_verifier(), stream=stream, scope=scope, now=self.clock.now()
        )

    def add_past_rows(self, at: datetime, count: int = 3, stream: Stream = Stream.AUDIT_LOG) -> None:
        """Records of an earlier moment, written directly (the writer stamps the present)."""
        documents = []
        for second in range(count):
            moment = at + timedelta(seconds=second)
            record: AuditLogRecord | SecurityEventRecord
            if stream is Stream.AUDIT_LOG:
                record = AuditLogRecord(
                    organization_id=self.organization,
                    recorded_at=moment,
                    month=canonical.month_key(moment),
                    occurred_at=moment,
                    actor_type="system",
                    action="test.past.event",
                    outcome="success",
                )
            else:
                record = SecurityEventRecord(
                    organization_id=self.organization,
                    recorded_at=moment,
                    month=canonical.month_key(moment),
                    occurred_at=moment,
                    event_type="login.failed",
                    severity="info",
                )
            documents.append(to_document(record))
        self.db[RECORDS[stream]].insert_many(documents)

    def links(self, stream: Stream = Stream.AUDIT_LOG, **query: Any) -> list[dict[str, Any]]:
        return list(self.db[LINKS].find({"stream": stream.value, **query}, sort=[("position", 1)]))

    def record_at(self, position: int) -> uuid.UUID:
        [link] = self.links(position=position)
        return (
            uuid.UUID(bytes=bytes(link["record_id"]))
            if isinstance(link["record_id"], bytes)
            else link["record_id"]
        )


def checks(report: Report) -> set[str]:
    return {finding.check for finding in report.findings}


@pytest.fixture
async def env(migrated_mongo: MongoServer) -> AsyncIterator[Env]:
    name = await fresh_migrated_database(migrated_mongo)
    organization = await create_organization(migrated_mongo.uri("hrms_app"), name)
    app, worker, retention_db = (
        Database(create_client(migrated_mongo.uri(user), application_name=f"test-{user}"), name)
        for user in ("hrms_app", "hrms_worker", "hrms_audit_retention")
    )
    root = migrated_mongo.admin()
    try:
        yield Env(migrated_mongo, name, organization, app, worker, retention_db, root, FakeClock())
    finally:
        for database in (app, worker, retention_db):
            await database.dispose()
        root.close()


# --- sealing and checkpoints -----------------------------------------------------------------------


async def test_records_are_sealed_checkpointed_and_signed(env: Env) -> None:
    await env.write(5)
    integrity = env.integrity()
    assert await integrity.seal() == 10
    assert await integrity.seal() == 0
    assert await integrity.checkpoint() >= 2

    key = canonical.month_key(env.clock.now())
    for stream in Stream:
        assert [link["position"] for link in env.links(stream, partition_key=key)] == [1, 2, 3, 4, 5]
        async with env.worker.unit_of_work() as session:
            [checkpoint] = [
                item for item in await chain.checkpoints(session, stream) if item.fields.partition_key == key
            ]
        assert checkpoint.fields.kind is CheckpointKind.DAILY
        assert checkpoint.fields.last_position == 5
        assert audit_verifier().verify("test-key", checkpoint.checkpoint_hash, checkpoint.signature)
        for scope in Scope:
            report = await env.verify(stream, scope)
            assert report.ok, report.findings
            assert report.rows_checked == 5
    # One daily checkpoint per month per day, and none when nothing changed.
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
    for stream in Stream:
        links = env.links(stream)
        assert [link["position"] for link in links] == list(range(1, 41))
        assert len({link["record_id"] for link in links}) == 40
    await integrity.checkpoint()
    assert (await env.verify()).ok


async def test_the_chain_is_insert_only_and_written_only_by_the_jobs(env: Env) -> None:
    """The Atlas custom roles (scripts/mongo_roles.py) as the test users hold them."""
    await env.write(1)
    integrity = env.integrity()
    await integrity.seal()
    await integrity.checkpoint()
    [link] = env.links(position=1)
    probe = {"stream": "test", "partition_key": "2026-01", "position": 1}

    # The API's user reads the chain but writes nothing to it.
    for collection in (LINKS, CHECKPOINTS):
        with pytest.raises(OperationFailure, match="not authorized"):
            await env.app.collection(collection).insert_one(dict(probe))
    # Nobody changes a link or a checkpoint; only retention removes links, and only it.
    for database in (env.app, env.worker, env.retention):
        for collection in (LINKS, CHECKPOINTS):
            with pytest.raises(OperationFailure, match="not authorized"):
                await database.collection(collection).update_one({}, {"$set": {"position": 0}})
    for database in (env.app, env.worker):
        for collection in (LINKS, CHECKPOINTS):
            with pytest.raises(OperationFailure, match="not authorized"):
                await database.collection(collection).delete_one({"_id": link["_id"]})
    with pytest.raises(OperationFailure, match="not authorized"):
        await env.retention.collection(CHECKPOINTS).delete_many({})
    # The audit records themselves: the API and the worker only add; retention only removes.
    for database in (env.app, env.worker):
        for collection in RECORDS.values():
            with pytest.raises(OperationFailure, match="not authorized"):
                await database.collection(collection).update_many({}, {"$set": {"reason": "x"}})
            with pytest.raises(OperationFailure, match="not authorized"):
                await database.collection(collection).delete_many({})
    assert (await env.verify()).ok


# --- month boundaries and partition-final checkpoints --------------------------------------------


async def test_months_close_with_a_final_checkpoint_and_later_rows_are_tampering(env: Env) -> None:
    env.add_past_rows(datetime(2025, 1, 31, 23, 59, 58, tzinfo=UTC), 2)  # the last seconds of January
    env.add_past_rows(datetime(2025, 2, 1, tzinfo=UTC), 1)  # the first instant of February
    integrity = env.integrity()

    # Within the margin after the month ends, January is still open: a daily checkpoint only.
    env.clock.current = datetime(2025, 2, 1, 0, 30, tzinfo=UTC)
    await integrity.seal()
    await integrity.checkpoint()
    assert [len(env.links(partition_key=key)) for key in ("2025-01", "2025-02")] == [2, 1]
    kinds = sorted(
        (item["partition_key"], item["kind"]) for item in env.db[CHECKPOINTS].find({"stream": "audit_log"})
    )
    assert kinds == [("2025-01", "daily"), ("2025-02", "daily")]

    # A record committed late, inside the margin, is still sealed before the month closes.
    env.add_past_rows(datetime(2025, 1, 31, 23, 59, 59, 999000, tzinfo=UTC), 1)
    env.clock.current = datetime(2025, 2, 1, 1, 0, 1, tzinfo=UTC)
    await integrity.checkpoint()
    [final] = env.db[CHECKPOINTS].find({"kind": "partition_final", "stream": "audit_log"})
    assert final["last_position"] == 3
    report = await env.verify()
    assert report.ok, report.findings

    # After the final checkpoint, nothing more is sealed there: a new record is tampering.
    env.add_past_rows(datetime(2025, 1, 20, tzinfo=UTC), 1)
    await integrity.seal()
    assert len(env.links(partition_key="2025-01")) == 3
    assert checks(await env.verify()) == {"rows_after_final"}
    # A month that closed long ago without a final checkpoint means the job stopped.
    env.clock.current = datetime(2025, 3, 3, tzinfo=UTC)
    assert "partition_not_final" in checks(await env.verify())


async def test_records_left_unsealed_are_reported(env: Env) -> None:
    await env.write(2)
    env.clock.advance(UNSEALED_ALERT + timedelta(minutes=1))
    report = await env.verify(scope=Scope.RECENT)
    assert checks(report) == {"unsealed_rows"}
    await env.integrity().seal()
    assert (await env.verify(scope=Scope.RECENT)).ok


# --- tamper suite -------------------------------------------------------------------------------


async def sealed(env: Env, count: int = 5) -> AuditIntegrity:
    await env.write(count)
    integrity = env.integrity()
    await integrity.seal()
    await integrity.checkpoint()
    assert (await env.verify()).ok
    return integrity


async def test_a_modified_record_is_detected(env: Env) -> None:
    await sealed(env)
    env.db["audit_log"].update_one({"_id": env.record_at(3)}, {"$set": {"reason": "nothing happened"}})
    report = await env.verify()
    assert checks(report) == {"row_modified"}
    assert [finding.detail for finding in report.findings] == ["position 3"]


async def test_a_modified_security_event_is_detected(env: Env) -> None:
    await sealed(env)
    env.db["security_events"].update_one(
        {"email_attempted_hash": bytes([2]) * 32}, {"$set": {"severity": "info"}}
    )
    assert checks(await env.verify(Stream.SECURITY_EVENTS)) == {"row_modified"}


async def test_a_deleted_record_is_detected(env: Env) -> None:
    await sealed(env)
    env.db["audit_log"].delete_one({"_id": env.record_at(2)})
    assert checks(await env.verify()) == {"row_missing"}


async def test_reordered_records_are_detected(env: Env) -> None:
    await sealed(env)
    second, third = env.record_at(2), env.record_at(3)
    links = env.db[LINKS]
    # A placeholder first: the record of a month is unique in its chain.
    links.update_one({"stream": "audit_log", "position": 2}, {"$set": {"record_id": uuid.uuid4()}})
    links.update_one({"stream": "audit_log", "position": 3}, {"$set": {"record_id": second}})
    links.update_one({"stream": "audit_log", "position": 2}, {"$set": {"record_id": third}})
    report = await env.verify()
    assert checks(report) == {"row_modified"}
    assert sorted(finding.detail for finding in report.findings) == ["position 2", "position 3"]


async def test_a_truncated_tail_is_detected(env: Env) -> None:
    await sealed(env)
    env.db[LINKS].delete_many({"stream": "audit_log", "position": {"$gt": 3}})
    assert "checkpoint_beyond_chain" in checks(await env.verify())


async def test_a_removed_link_is_detected(env: Env) -> None:
    await sealed(env)
    env.db[LINKS].delete_one({"stream": "audit_log", "position": 2})
    assert {"link_gap", "link_broken"} <= checks(await env.verify())


async def test_a_chain_rewritten_without_the_signing_key_is_detected(env: Env) -> None:
    """The attacker edits a record, then recomputes every hash the database holds: the record's
    digest, every later link and the checkpoint. Only the signature cannot follow."""
    await sealed(env)
    key = canonical.month_key(env.clock.now())
    env.db["audit_log"].update_one({"_id": env.record_at(2)}, {"$set": {"reason": "rewritten"}})
    async with env.worker.unit_of_work() as session:
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
        env.db[LINKS].update_one(
            {"stream": "audit_log", "position": link.position},
            {"$set": {"row_digest": digest, "link_hash": previous}},
        )
    forged = replace(checkpoint.fields, last_link_hash=previous)
    number = {"stream": "audit_log", "checkpoint_no": checkpoint.fields.checkpoint_no}
    env.db[CHECKPOINTS].update_one(
        number, {"$set": {"last_link_hash": previous, "checkpoint_hash": canonical.checkpoint_hash(forged)}}
    )
    assert checks(await env.verify()) == {"checkpoint_signature_invalid"}
    # Signing with a key the verifier does not trust fails the same way.
    stranger = audit_signer("test-key", b"x" * 32)
    env.db[CHECKPOINTS].update_one(
        number, {"$set": {"signature": stranger.sign(canonical.checkpoint_hash(forged))}}
    )
    assert checks(await env.verify()) == {"checkpoint_signature_invalid"}


async def test_a_deleted_checkpoint_in_the_middle_is_a_gap(env: Env) -> None:
    integrity = await sealed(env)
    for _ in range(2):
        await env.write(1)
        await integrity.seal()
        env.clock.advance(timedelta(days=1))
        await integrity.checkpoint()
    env.db[CHECKPOINTS].delete_one({"stream": "audit_log", "checkpoint_no": 2})
    assert {"checkpoint_gap", "checkpoint_chain_broken"} <= checks(await env.verify())


async def test_a_month_removed_outside_retention_is_detected(env: Env) -> None:
    env.add_past_rows(datetime(2025, 1, 10, tzinfo=UTC), 2)
    env.clock.current = datetime(2025, 2, 2, tzinfo=UTC)
    integrity = env.integrity()
    await integrity.seal()
    await integrity.checkpoint()
    assert (await env.verify()).ok
    env.db["audit_log"].delete_many({"month": "2025-01"})
    # The month is missing, and every link of it names a record that is gone.
    assert checks(await env.verify()) == {"partition_missing", "row_missing"}


async def test_a_month_removed_before_its_first_checkpoint_is_detected(env: Env) -> None:
    env.add_past_rows(datetime(2025, 1, 10, tzinfo=UTC), 2)
    env.clock.current = datetime(2025, 1, 10, 0, 5, tzinfo=UTC)
    await env.integrity().seal()
    env.db["audit_log"].delete_many({"month": "2025-01"})
    report = await env.verify()
    assert checks(report) == {"partition_missing", "row_missing"}
    assert {finding.partition_key for finding in report.findings} == {"2025-01"}


# --- the witness: what the database alone cannot show ----------------------------------------


async def witnessed(env: Env, integrity: AuditIntegrity) -> list[witness.WitnessLine]:
    """Send the daily witness and read the lines back from the email, as an auditor saves them."""
    sender = RecordingSender()
    assert (
        await witness.send_witness(
            env.worker, sender, ["auditor@dev.example"], env.clock.now(), verifier=audit_verifier()
        )
        == 2
    )
    [email] = sender.sent
    return witness.parse_lines(email.body.splitlines())


async def test_the_newest_checkpoints_deleted_with_their_records_are_caught_by_the_witness(env: Env) -> None:
    """Removing the newest records together with their links and checkpoints leaves a shorter
    chain that verifies in the database. Only a copy kept outside it shows the loss."""
    integrity = await sealed(env)
    await env.write(2)
    await integrity.seal()
    env.clock.advance(timedelta(days=1))
    await integrity.checkpoint()
    lines = await witnessed(env, integrity)
    assert await witness.verify_witness(env.worker, audit_verifier(), lines) == []

    newest = env.db[CHECKPOINTS].find_one({"stream": "audit_log"}, sort=[("checkpoint_no", -1)])
    assert newest is not None
    key, position = newest["partition_key"], newest["last_position"]
    removed = [link["record_id"] for link in env.links(partition_key=key, position={"$gt": 5})]
    env.db["audit_log"].delete_many({"_id": {"$in": removed}})
    env.db[LINKS].delete_many({"stream": "audit_log", "partition_key": key, "position": {"$gt": 5}})
    env.db[CHECKPOINTS].delete_one({"_id": newest["_id"]})
    assert position == 7

    for scope in Scope:
        assert (await env.verify(scope=scope)).ok  # the database alone cannot tell
    findings = await witness.verify_witness(env.worker, audit_verifier(), lines)
    assert [(item.check, item.stream) for item in findings] == [("witnessed_checkpoint_missing", "audit_log")]


async def test_a_forged_or_changed_witness_line_is_reported(env: Env) -> None:
    integrity = await sealed(env)
    [line, other] = await witnessed(env, integrity)
    forged = replace(line, signature=audit_signer("test-key", b"x" * 32).sign(line.checkpoint_hash))
    changed = replace(other, checkpoint_hash=bytes(32), signature=audit_signer().sign(bytes(32)))
    findings = await witness.verify_witness(env.worker, audit_verifier(), [forged, changed])
    assert [item.check for item in findings] == ["witness_signature_invalid", "witnessed_checkpoint_changed"]


async def test_the_witness_is_sent_daily_or_its_absence_is_logged(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    from app.platform.jobs import Scheduler  # noqa: PLC0415

    await sealed(env)
    sender = RecordingSender()
    for recipients in ((), ("auditor@dev.example", "second@dev.example")):
        scheduler = Scheduler(env.worker, env.clock, owner=f"test-{len(recipients)}")
        witness.register_audit_witness(
            scheduler,
            env.worker,
            sender=sender,
            recipients=recipients,
            verifier=audit_verifier(),
            clock=env.clock,
        )
        env.db["job_leases"].delete_many({})
        assert await scheduler.run_due() == [witness.TASK_NAME]
    assert "audit.witness_not_configured" in [record.msg for record in caplog.records]
    assert sorted(email.to for email in sender.sent) == ["auditor@dev.example", "second@dev.example"]
    assert all(len(witness.parse_lines(email.body.splitlines())) == 2 for email in sender.sent)


async def test_a_head_whose_checkpoints_do_not_verify_is_not_witnessed(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    await sealed(env)
    newest = env.db[CHECKPOINTS].find_one({"stream": "audit_log"}, sort=[("checkpoint_no", -1)])
    assert newest is not None
    # A checkpoint changed in the database: its hash no longer matches its fields or signature.
    env.db[CHECKPOINTS].update_one({"_id": newest["_id"]}, {"$set": {"last_position": 999}})
    sender = RecordingSender()
    sent = await witness.send_witness(
        env.worker, sender, ["auditor@dev.example"], env.clock.now(), verifier=audit_verifier()
    )
    assert sent == 1
    [email] = sender.sent
    lines = witness.parse_lines(email.body.splitlines())
    assert [line.stream for line in lines] == [Stream.SECURITY_EVENTS]
    assert "Not witnessed: the signed checkpoints of the audit_log stream did not verify" in email.body
    assert [r.levelname for r in caplog.records if r.msg == "audit.witness_withheld"] == ["ERROR"]


# --- retention ------------------------------------------------------------------------------------


def months_ready_for_retention(env: Env) -> None:
    for month in (1, 2, 3):
        env.add_past_rows(datetime(2025, month, 10, tzinfo=UTC), 2)
        env.add_past_rows(datetime(2025, month, 10, tzinfo=UTC), 2, stream=Stream.SECURITY_EVENTS)


async def test_retention_removes_a_month_only_after_a_signed_tombstone(env: Env) -> None:
    months_ready_for_retention(env)
    integrity = env.integrity(months=1)
    env.clock.current = datetime(2025, 4, 15, tzinfo=UTC)
    await integrity.seal()
    await integrity.checkpoint()

    # Without its tombstone nothing is removed.
    with pytest.raises(RuntimeError, match="no retention tombstone"):
        await retention._remove_month(env.retention, Stream.AUDIT_LOG, "2025-01")
    assert env.db["audit_log"].count_documents({"month": "2025-01"}) == 2

    results = await integrity.retention()
    assert [(result.stream, result.removed) for result in results] == [
        (Stream.AUDIT_LOG, ["2025-01", "2025-02"]),
        (Stream.SECURITY_EVENTS, ["2025-01", "2025-02"]),
    ]
    tombstones = sorted(
        (item["stream"], item["partition_key"], item["last_position"], item["retention_policy_version"])
        for item in env.db[CHECKPOINTS].find({"kind": "retention_tombstone"})
    )
    assert tombstones == [
        ("audit_log", "2025-01", 2, "retain_months=1"),
        ("audit_log", "2025-02", 2, "retain_months=1"),
        ("security_events", "2025-01", 2, "retain_months=1"),
        ("security_events", "2025-02", 2, "retain_months=1"),
    ]
    for stream, collection in RECORDS.items():
        assert sorted(env.db[collection].distinct("month")) == ["2025-03"]
        assert sorted(env.db[LINKS].distinct("partition_key", {"stream": stream.value})) == ["2025-03"]
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
    env.db["audit_log"].update_many({"month": "2025-01"}, {"$set": {"action": "test.past.other"}})

    results = await integrity.retention()
    assert results[0].removed == []
    assert results[0].failed_verification is not None
    assert checks(results[0].failed_verification) == {"row_modified"}
    assert env.db["audit_log"].count_documents({"month": "2025-01"}) == 2
    assert env.db[CHECKPOINTS].count_documents({"kind": "retention_tombstone", "stream": "audit_log"}) == 0
    assert [report.stream for report in env.mismatches] == [Stream.AUDIT_LOG]
    [event] = env.db["security_events"].find({"event_type": "audit.chain_mismatch", "severity": "high"})
    assert event["details"] == {
        "stream": "audit_log",
        "scope": "full",
        "findings": 2,
        "checks": "row_modified",
    }


async def test_retention_resumes_after_a_tombstone_was_written(env: Env) -> None:
    months_ready_for_retention(env)
    integrity = env.integrity(months=1)
    env.clock.current = datetime(2025, 4, 15, tzinfo=UTC)
    await integrity.seal()
    await integrity.checkpoint()
    # A run that stopped after the tombstone committed, before the removal.
    async with env.retention.unit_of_work() as session:
        [final] = [
            item
            for item in await chain.checkpoints(session, Stream.AUDIT_LOG)
            if item.fields.kind is CheckpointKind.PARTITION_FINAL and item.fields.partition_key == "2025-01"
        ]
    await retention._append_tombstone(env.retention, audit_signer(), final, 1, env.clock.now())
    assert env.db[CHECKPOINTS].count_documents({"kind": "retention_tombstone"}) == 1
    assert env.db["audit_log"].count_documents({"month": "2025-01"}) == 2

    result = await retention.apply_retention(
        env.retention,
        audit_signer(),
        audit_verifier(),
        stream=Stream.AUDIT_LOG,
        months=1,
        now=env.clock.now(),
    )
    assert result.removed == ["2025-01", "2025-02"]
    assert env.db[CHECKPOINTS].count_documents({"kind": "retention_tombstone"}) == 2
    assert (await env.verify()).ok


# --- alerting and the operator command ----------------------------------------------------------


async def test_verification_failures_are_reported_once_per_stream(env: Env) -> None:
    await sealed(env)
    env.db["audit_log"].update_one({"_id": env.record_at(1)}, {"$set": {"reason": "changed"}})
    reports = await env.integrity().verify(Scope.RECENT)
    assert [(report.stream, report.ok) for report in reports] == [
        (Stream.AUDIT_LOG, False),
        (Stream.SECURITY_EVENTS, True),
    ]
    assert [report.stream for report in env.mismatches] == [Stream.AUDIT_LOG]
    [event] = env.db["security_events"].find({"event_type": "audit.chain_mismatch"})
    assert event["severity"] == "high"
    assert event["details"] == {
        "stream": "audit_log",
        "scope": "recent",
        "findings": 1,
        "checks": "row_modified",
    }


async def test_the_audit_verify_command_prints_every_problem(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    integrity = await sealed(env)
    lines = await witnessed(env, integrity)
    settings = AuditVerifySettings(
        app_env=AppEnv.TEST,
        mongodb_uri=env.mongo.uri("hrms_worker"),
        mongodb_database=env.name,
        audit_verify_public_keys=audit_verify_keys_json(),
    )
    reports = await cli.audit_verify(settings, Scope.FULL)
    assert [(report.stream, report.ok) for report in reports] == [
        (Stream.AUDIT_LOG, True),
        (Stream.SECURITY_EVENTS, True),
    ]
    assert await cli.verify_witness_lines(settings, lines) == 0
    env.db["audit_log"].update_one({"_id": env.record_at(2)}, {"$set": {"reason": "changed"}})
    reports = await cli.audit_verify(settings, Scope.FULL)
    cli._print_reports(reports)
    output = capsys.readouterr().out
    key = canonical.month_key(env.clock.now())
    assert "audit_log: FAILED (full; " in output
    assert f"  row_modified [{key}]: position 2\n" in output
    assert "security_events: OK" in output
    assert "witness: OK (2 witnessed checkpoints)" in output
    # The report names positions and checks, never record contents.
    assert "changed" not in output
