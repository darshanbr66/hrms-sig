"""Reads and writes of the audit integrity chain, shared by the sealer, the checkpoint writer,
verification and retention (docs/security-architecture.md §8.1).

The chain spans every organization: it covers the stored records themselves, read straight
from their collections (never through a tenant-scoped query), so a record of any organization
that is changed or removed breaks it. Writers serialize per stream on lock documents
(`audit_locks`): one for sealing (links) and one for the checkpoint chain, which the
checkpoint job and the retention job (different database users) both take.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pymongo.errors import PyMongoError

from app.platform.audit import canonical
from app.platform.audit.canonical import CheckpointFields, CheckpointKind, Stream
from app.platform.audit.signing import CheckpointSigner
from app.platform.audit.tables import (
    AuditLock,
    AuditLogRecord,
    ChainCheckpoint,
    ChainLink,
    SecurityEventRecord,
)
from app.platform.db import ID, Document, Session, translate_error, truncate

RECORDS: Final[dict[Stream, type[Document]]] = {
    Stream.AUDIT_LOG: AuditLogRecord,
    Stream.SECURITY_EVENTS: SecurityEventRecord,
}


def records_collection(session: Session, stream: Stream) -> Any:
    return session.collection(RECORDS[stream])


async def lock(session: Session, name: str) -> None:
    """Take a lock for the rest of the transaction: a concurrent holder makes one of the two
    transactions fail with `ConcurrentChangeError`; the jobs run again (`retry_on_conflict`)."""
    try:
        await session.collection(AuditLock).update_one(
            {ID: name}, {"$inc": {"taken": 1}}, upsert=True, session=session.client_session
        )
    except PyMongoError as exc:
        raise translate_error(exc) from exc


def seal_lock(stream: Stream) -> str:
    return f"audit.seal:{stream.value}"


def checkpoint_lock(stream: Stream) -> str:
    return f"audit.checkpoints:{stream.value}"


# --- months ---------------------------------------------------------------------------------


async def record_months(session: Session, stream: Stream) -> list[str]:
    """The months that hold records of the stream."""
    values = await records_collection(session, stream).distinct("month", {}, session=session.client_session)
    return sorted(str(value) for value in values)


async def link_months(session: Session, stream: Stream) -> list[str]:
    """The months that hold chain links of the stream."""
    values = await session.collection(ChainLink).distinct(
        "partition_key", {"stream": stream.value}, session=session.client_session
    )
    return sorted(str(value) for value in values)


# --- links ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tail:
    position: int
    link_hash: bytes


async def tail(session: Session, stream: Stream, key: str) -> Tail:
    """The last link of a month's chain, or position 0 and the genesis."""
    stored = await session.collection(ChainLink).find_one(
        {"stream": stream.value, "partition_key": key},
        sort=[("position", -1)],
        session=session.client_session,
    )
    if stored is None:
        return Tail(0, canonical.genesis(stream, key))
    return Tail(int(stored["position"]), bytes(stored["link_hash"]))


def _unlinked_pipeline(stream: Stream, key: str, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"month": key}
    if extra:
        match.update(extra)
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": ChainLink.__collection__,
                "let": {"record": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "stream": stream.value,
                            "partition_key": key,
                            "$expr": {"$eq": ["$record_id", "$$record"]},
                        }
                    },
                    {"$project": {"_id": 1}},
                ],
                "as": "link",
            }
        },
        {"$match": {"link": {"$size": 0}}},
    ]


@dataclass(frozen=True, slots=True)
class Row:
    id: uuid.UUID
    values: dict[str, Any]


async def unsealed_rows(session: Session, stream: Stream, key: str, limit: int) -> list[Row]:
    """Committed records of the month with no link yet, oldest first."""
    pipeline = [*_unlinked_pipeline(stream, key), {"$sort": {"recorded_at": 1, ID: 1}}, {"$limit": limit}]
    cursor = await records_collection(session, stream).aggregate(pipeline, session=session.client_session)
    return [Row(item[ID], canonical.row_values(stream, item)) async for item in cursor]


@dataclass(frozen=True, slots=True)
class UnsealedSummary:
    count: int
    oldest: datetime | None


async def unsealed_summary(
    session: Session, stream: Stream, key: str, *, recorded_before: datetime | None = None
) -> UnsealedSummary:
    extra = {"recorded_at": {"$lt": recorded_before}} if recorded_before is not None else None
    pipeline = [
        *_unlinked_pipeline(stream, key, extra),
        {"$group": {"_id": None, "count": {"$sum": 1}, "oldest": {"$min": "$recorded_at"}}},
    ]
    cursor = await records_collection(session, stream).aggregate(pipeline, session=session.client_session)
    found = [item async for item in cursor]
    if not found:
        return UnsealedSummary(0, None)
    return UnsealedSummary(int(found[0]["count"]), found[0]["oldest"])


@dataclass(frozen=True, slots=True)
class NewLink:
    position: int
    record_id: uuid.UUID
    row_digest: bytes
    link_hash: bytes


async def insert_links(session: Session, stream: Stream, key: str, links: Sequence[NewLink]) -> None:
    session.add_all(
        [
            ChainLink(
                stream=stream.value,
                partition_key=key,
                position=link.position,
                record_id=link.record_id,
                digest_version=canonical.DIGEST_VERSION,
                row_digest=link.row_digest,
                link_hash=link.link_hash,
            )
            for link in links
        ]
    )
    await session.flush()


@dataclass(frozen=True, slots=True)
class StoredLink:
    position: int
    record_id: uuid.UUID
    digest_version: int
    row_digest: bytes
    link_hash: bytes
    # The record the link names, canonical; None when it is not in the month.
    row: dict[str, Any] | None


async def links_with_rows(
    session: Session, stream: Stream, key: str, *, after: int, limit: int
) -> list[StoredLink]:
    """The next `limit` links of a month after position `after`, with the records they name,
    in position order."""
    cursor = session.collection(ChainLink).find(
        {"stream": stream.value, "partition_key": key, "position": {"$gt": after}},
        sort=[("position", 1)],
        limit=limit,
        session=session.client_session,
    )
    links = [item async for item in cursor]
    ids = [item["record_id"] for item in links]
    rows: dict[uuid.UUID, dict[str, Any]] = {}
    if ids:
        found = records_collection(session, stream).find(
            {ID: {"$in": ids}, "month": key}, session=session.client_session
        )
        rows = {item[ID]: canonical.row_values(stream, item) async for item in found}
    return [
        StoredLink(
            int(item["position"]),
            item["record_id"],
            int(item["digest_version"]),
            bytes(item["row_digest"]),
            bytes(item["link_hash"]),
            rows.get(item["record_id"]),
        )
        for item in links
    ]


# --- checkpoints ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    fields: CheckpointFields
    checkpoint_hash: bytes
    signature: bytes

    @property
    def body(self) -> bytes:
        return canonical.export_body(self.fields, self.signature)


def _stored(values: dict[str, Any]) -> StoredCheckpoint:
    fields = CheckpointFields(
        stream=Stream(values["stream"]),
        checkpoint_no=int(values["checkpoint_no"]),
        kind=CheckpointKind(values["kind"]),
        partition_key=values["partition_key"],
        last_position=int(values["last_position"]),
        row_count=int(values["row_count"]),
        last_link_hash=bytes(values["last_link_hash"]),
        prev_checkpoint_hash=bytes(values["prev_checkpoint_hash"]),
        key_id=values["key_id"],
        created_at=values["created_at"],
        archive_sha256=bytes(values["archive_sha256"]) if values.get("archive_sha256") is not None else None,
        retention_policy_version=values.get("retention_policy_version"),
    )
    return StoredCheckpoint(fields, bytes(values["checkpoint_hash"]), bytes(values["signature"]))


async def checkpoints(
    session: Session, stream: Stream, *, created_since: datetime | None = None
) -> list[StoredCheckpoint]:
    query: dict[str, Any] = {"stream": stream.value}
    if created_since is not None:
        query["created_at"] = {"$gte": created_since}
    cursor = session.collection(ChainCheckpoint).find(
        query, sort=[("checkpoint_no", 1)], session=session.client_session
    )
    return [_stored(item) async for item in cursor]


async def append_checkpoint(
    session: Session,
    signer: CheckpointSigner,
    *,
    stream: Stream,
    kind: CheckpointKind,
    partition_key: str,
    end: Tail,
    now: datetime,
    retention_policy_version: str | None = None,
) -> StoredCheckpoint:
    """Sign and insert the next checkpoint of the stream's chain. Takes the checkpoint lock,
    so the sealer's checkpoints and retention tombstones never race for a number."""
    await lock(session, checkpoint_lock(stream))
    # Stored at millisecond precision, so it is hashed at that precision too.
    now = truncate(now)
    previous = await session.collection(ChainCheckpoint).find_one(
        {"stream": stream.value}, sort=[("checkpoint_no", -1)], session=session.client_session
    )
    fields = CheckpointFields(
        stream=stream,
        checkpoint_no=int(previous["checkpoint_no"]) + 1 if previous else 1,
        kind=kind,
        partition_key=partition_key,
        last_position=end.position,
        row_count=end.position,
        last_link_hash=end.link_hash,
        prev_checkpoint_hash=bytes(previous["checkpoint_hash"]) if previous else canonical.ZERO_HASH,
        key_id=signer.key_id,
        created_at=now,
        retention_policy_version=retention_policy_version,
    )
    digest = canonical.checkpoint_hash(fields)
    signature = signer.sign(digest)
    session.add(
        ChainCheckpoint(
            stream=stream.value,
            checkpoint_no=fields.checkpoint_no,
            kind=kind.value,
            partition_key=partition_key,
            last_position=fields.last_position,
            row_count=fields.row_count,
            last_link_hash=fields.last_link_hash,
            prev_checkpoint_hash=fields.prev_checkpoint_hash,
            checkpoint_hash=digest,
            signature=signature,
            key_id=signer.key_id,
            retention_policy_version=retention_policy_version,
            created_at=now,
        )
    )
    await session.flush()
    return StoredCheckpoint(fields, digest, signature)
