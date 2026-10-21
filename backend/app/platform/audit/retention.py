"""Audit retention with tombstones (docs/security-architecture.md §8.1, "Retention").

Runs as its own database user (`MONGODB_AUDIT_RETENTION_URI`), the only one with the remove
privilege on the audit collections, in the worker process (where the signing key is). For one
stream and its retention in months:

1. A month is eligible when it ended at least the retention period ago and its
   partition-final checkpoint exists. Oldest first.
2. The stream's checkpoint chain and the month's records are verified in full. Any finding
   stops retention for the stream; the findings are returned for alerting.
3. A signed `retention_tombstone` checkpoint repeating the final checkpoint's position and
   link hash is appended to the chain.
4. Only then are the month's records and chain links removed, in a transaction that first
   confirms the tombstone exists.

A tombstone already in the chain (a run that stopped after step 3) means the removal is
resumed without verifying again. Archiving before removal is not configured, so tombstones
carry no archive hash.

The retention period is configuration (`AUDIT_LOG_RETENTION_MONTHS`,
`SECURITY_EVENTS_RETENTION_MONTHS`); without it, nothing is removed.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

from app.platform.audit import canonical, chain
from app.platform.audit.canonical import CheckpointKind, Stream
from app.platform.audit.chain import StoredCheckpoint
from app.platform.audit.signing import CheckpointSigner, SignatureVerifier
from app.platform.audit.tables import ChainCheckpoint, ChainLink
from app.platform.audit.verify import Report, Scope, verify_stream
from app.platform.db import Database

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetentionResult:
    stream: Stream
    removed: list[str] = field(default_factory=list)
    # Set when verification failed, which stops retention for the stream.
    failed_verification: Report | None = None


def policy_version(months: int) -> str:
    return f"retain_months={months}"


def eligible(key: str, months: int, now: datetime) -> bool:
    return canonical.add_months(canonical.month_end(key), months) <= now


async def apply_retention(
    database: Database,
    signer: CheckpointSigner,
    verifier: SignatureVerifier,
    *,
    stream: Stream,
    months: int,
    now: datetime,
) -> RetentionResult:
    if months < 1:
        raise ValueError("a retention period is at least one month")
    result = RetentionResult(stream)
    async with database.unit_of_work() as session:
        keys = sorted(
            set(await chain.record_months(session, stream)) | set(await chain.link_months(session, stream))
        )
        stored = await chain.checkpoints(session, stream)
    finals = {
        item.fields.partition_key: item
        for item in stored
        if item.fields.kind is CheckpointKind.PARTITION_FINAL
    }
    tombstones = {
        item.fields.partition_key: item
        for item in stored
        if item.fields.kind is CheckpointKind.RETENTION_TOMBSTONE
    }
    for key in keys:
        if not eligible(key, months, now):
            continue
        final = finals.get(key)
        if final is None:
            logger.warning("audit.retention_waiting_for_final", extra={"stream": stream.value, "month": key})
            continue
        if key not in tombstones:
            report = await verify_stream(
                database, verifier, stream=stream, scope=Scope.FULL, now=now, only_partitions=[key]
            )
            if not report.ok:
                result.failed_verification = report
                return result
            await database.retry_on_conflict(partial(_append_tombstone, database, signer, final, months, now))
        await database.retry_on_conflict(partial(_remove_month, database, stream, key))
        result.removed.append(key)
        logger.info("audit.partition_removed", extra={"stream": stream.value, "month": key})
    return result


async def _append_tombstone(
    database: Database, signer: CheckpointSigner, final: StoredCheckpoint, months: int, now: datetime
) -> StoredCheckpoint:
    async with database.unit_of_work() as session:
        return await chain.append_checkpoint(
            session,
            signer,
            stream=final.fields.stream,
            kind=CheckpointKind.RETENTION_TOMBSTONE,
            partition_key=final.fields.partition_key,
            end=chain.Tail(final.fields.last_position, final.fields.last_link_hash),
            now=now,
            retention_policy_version=policy_version(months),
        )


async def _remove_month(database: Database, stream: Stream, key: str) -> None:
    """Remove a month's records and links; refused unless its tombstone is in the chain."""
    async with database.unit_of_work() as session:
        await chain.lock(session, chain.seal_lock(stream))
        tombstone = await session.collection(ChainCheckpoint).find_one(
            {"stream": stream.value, "partition_key": key, "kind": CheckpointKind.RETENTION_TOMBSTONE.value},
            session=session.client_session,
        )
        if tombstone is None:
            raise RuntimeError(f"no retention tombstone for {stream.value} {key}; nothing removed")
        await chain.records_collection(session, stream).delete_many(
            {"month": key}, session=session.client_session
        )
        await session.collection(ChainLink).delete_many(
            {"stream": stream.value, "partition_key": key}, session=session.client_session
        )
