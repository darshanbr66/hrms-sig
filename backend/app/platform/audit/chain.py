"""Reads and writes of the audit integrity chain, shared by the sealer, the checkpoint writer,
verification and retention (docs/security-architecture.md §8.1, revision 0013).

Rows are read as PostgreSQL text under fixed session settings (`canonical.ROW_TEXT_SETTINGS`)
for their digest. Writers serialize per stream with transaction-scoped advisory locks: one for
sealing (links) and one for the checkpoint chain, which the checkpoint job and the retention
job (different database roles) both take.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, Label, Table, Text, and_, cast, exists, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit import canonical
from app.platform.audit.canonical import CheckpointFields, CheckpointKind, Stream
from app.platform.audit.signing import CheckpointSigner
from app.platform.audit.tables import audit_log, chain_checkpoints, chain_links, security_events

TABLES: Final[dict[Stream, Table]] = {Stream.AUDIT_LOG: audit_log, Stream.SECURITY_EVENTS: security_events}

type RowValues = dict[str, str | None]


async def lock(session: AsyncSession, name: str) -> None:
    """A transaction-scoped advisory lock, shared by every role that takes the same name."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"), {"name": name})


def seal_lock(stream: Stream) -> str:
    return f"audit.seal:{stream.value}"


def checkpoint_lock(stream: Stream) -> str:
    return f"audit.checkpoints:{stream.value}"


async def use_row_text_settings(session: AsyncSession) -> None:
    for name, value in canonical.ROW_TEXT_SETTINGS.items():
        await session.execute(text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value})


# --- partitions -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Partitions:
    """Month keys of the partitions attached to a stream, and any other attached table."""

    keys: list[str]
    unexpected: list[str]


async def partitions(session: AsyncSession, stream: Stream) -> Partitions:
    rows = await session.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = to_regclass(:parent) ORDER BY c.relname"
        ),
        {"parent": f"audit.{stream.value}"},
    )
    keys: list[str] = []
    unexpected: list[str] = []
    for (name,) in rows:
        key = canonical.key_of_partition(stream, name)
        (keys if key is not None else unexpected).append(key if key is not None else name)
    return Partitions(sorted(keys), unexpected)


async def chain_partition_keys(session: AsyncSession, stream: Stream) -> list[str]:
    """Month keys of the chain partitions attached for a stream (`chain_links_<stream>_pYYYY_MM`)."""
    rows = await session.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = to_regclass(:parent) ORDER BY c.relname"
        ),
        {"parent": f"audit.chain_links_{stream.value}"},
    )
    keys = (canonical.key_of_partition(stream, name.removeprefix("chain_links_")) for (name,) in rows)
    return sorted(key for key in keys if key is not None)


# --- links ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tail:
    position: int
    link_hash: bytes


async def tail(session: AsyncSession, stream: Stream, key: str) -> Tail:
    """The last link of a partition's chain, or position 0 and the genesis."""
    row = (
        await session.execute(
            select(chain_links.c.position, chain_links.c.link_hash)
            .where(chain_links.c.stream == stream.value, chain_links.c.partition_key == key)
            .order_by(chain_links.c.position.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return Tail(0, canonical.genesis(stream, key))
    return Tail(int(row.position), bytes(row.link_hash))


def _row_text_columns(stream: Stream) -> list[Label[str]]:
    table = TABLES[stream]
    return [cast(table.c[name], Text).label(name) for name in canonical.COLUMNS[stream]]


def _in_month(stream: Stream, key: str) -> list[ColumnElement[bool]]:
    table = TABLES[stream]
    return [
        table.c.recorded_at >= canonical.month_start(key),
        table.c.recorded_at < canonical.month_end(key),
    ]


def _unlinked(stream: Stream, key: str) -> ColumnElement[bool]:
    table = TABLES[stream]
    return ~exists().where(
        chain_links.c.stream == stream.value,
        chain_links.c.partition_key == key,
        chain_links.c.record_id == table.c.id,
    )


@dataclass(frozen=True, slots=True)
class Row:
    id: uuid.UUID
    values: RowValues


async def unsealed_rows(session: AsyncSession, stream: Stream, key: str, limit: int) -> list[Row]:
    """Committed rows of the month with no link yet, oldest first. The caller has set the
    row text settings."""
    table = TABLES[stream]
    result = await session.execute(
        select(table.c.id, *_row_text_columns(stream))
        .where(*_in_month(stream, key), _unlinked(stream, key))
        .order_by(table.c.recorded_at, table.c.id)
        .limit(limit)
    )
    names = canonical.COLUMNS[stream]
    return [Row(row[0], {name: row[index + 1] for index, name in enumerate(names)}) for row in result]


@dataclass(frozen=True, slots=True)
class UnsealedSummary:
    count: int
    oldest: datetime | None


async def unsealed_summary(
    session: AsyncSession, stream: Stream, key: str, *, recorded_before: datetime | None = None
) -> UnsealedSummary:
    table = TABLES[stream]
    conditions = [*_in_month(stream, key), _unlinked(stream, key)]
    if recorded_before is not None:
        conditions.append(table.c.recorded_at < recorded_before)
    row = (
        await session.execute(select(func.count(), func.min(table.c.recorded_at)).where(*conditions))
    ).one()
    return UnsealedSummary(int(row[0]), row[1])


@dataclass(frozen=True, slots=True)
class NewLink:
    position: int
    record_id: uuid.UUID
    row_digest: bytes
    link_hash: bytes


async def insert_links(session: AsyncSession, stream: Stream, key: str, links: Sequence[NewLink]) -> None:
    await session.execute(
        insert(chain_links),
        [
            {
                "stream": stream.value,
                "partition_key": key,
                "position": link.position,
                "record_id": link.record_id,
                "digest_version": canonical.DIGEST_VERSION,
                "row_digest": link.row_digest,
                "link_hash": link.link_hash,
            }
            for link in links
        ],
    )


@dataclass(frozen=True, slots=True)
class StoredLink:
    position: int
    record_id: uuid.UUID
    digest_version: int
    row_digest: bytes
    link_hash: bytes
    # The row the link names, as text; None when it is not in the partition.
    row: RowValues | None


async def links_with_rows(
    session: AsyncSession, stream: Stream, key: str, *, after: int, limit: int
) -> list[StoredLink]:
    """The next `limit` links of a partition after position `after`, with the rows they name,
    in position order. A position appears twice when two rows carry the link's record ID. The
    caller has set the row text settings."""
    table = TABLES[stream]
    batch = (
        select(chain_links)
        .where(
            chain_links.c.stream == stream.value,
            chain_links.c.partition_key == key,
            chain_links.c.position > after,
        )
        .order_by(chain_links.c.position)
        .limit(limit)
        .subquery()
    )
    statement = (
        select(
            batch.c.position,
            batch.c.record_id,
            batch.c.digest_version,
            batch.c.row_digest,
            batch.c.link_hash,
            table.c.id.label("found_id"),
            *_row_text_columns(stream),
        )
        .select_from(
            batch.outerjoin(table, (table.c.id == batch.c.record_id) & and_(*_in_month(stream, key)))
        )
        .order_by(batch.c.position)
    )
    names = canonical.COLUMNS[stream]
    found: list[StoredLink] = []
    for row in await session.execute(statement):
        values = (
            {name: row[6 + index] for index, name in enumerate(names)} if row.found_id is not None else None
        )
        found.append(
            StoredLink(
                int(row.position),
                row.record_id,
                int(row.digest_version),
                bytes(row.row_digest),
                bytes(row.link_hash),
                values,
            )
        )
    return found


# --- checkpoints ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    fields: CheckpointFields
    checkpoint_hash: bytes
    signature: bytes
    anchor_object_key: str

    @property
    def body(self) -> bytes:
        return canonical.anchor_body(self.fields, self.signature)


def _stored(row: object) -> StoredCheckpoint:
    values = row._mapping  # type: ignore[attr-defined]
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
        archive_sha256=bytes(values["archive_sha256"]) if values["archive_sha256"] is not None else None,
        retention_policy_version=values["retention_policy_version"],
    )
    return StoredCheckpoint(
        fields, bytes(values["checkpoint_hash"]), bytes(values["signature"]), values["anchor_object_key"]
    )


async def checkpoints(
    session: AsyncSession, stream: Stream, *, created_since: datetime | None = None
) -> list[StoredCheckpoint]:
    statement = select(chain_checkpoints).where(chain_checkpoints.c.stream == stream.value)
    if created_since is not None:
        statement = statement.where(chain_checkpoints.c.created_at >= created_since)
    rows = await session.execute(statement.order_by(chain_checkpoints.c.checkpoint_no))
    return [_stored(row) for row in rows]


async def append_checkpoint(
    session: AsyncSession,
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
    previous = (
        await session.execute(
            select(chain_checkpoints.c.checkpoint_no, chain_checkpoints.c.checkpoint_hash)
            .where(chain_checkpoints.c.stream == stream.value)
            .order_by(chain_checkpoints.c.checkpoint_no.desc())
            .limit(1)
        )
    ).one_or_none()
    fields = CheckpointFields(
        stream=stream,
        checkpoint_no=int(previous.checkpoint_no) + 1 if previous else 1,
        kind=kind,
        partition_key=partition_key,
        last_position=end.position,
        row_count=end.position,
        last_link_hash=end.link_hash,
        prev_checkpoint_hash=bytes(previous.checkpoint_hash) if previous else canonical.ZERO_HASH,
        key_id=signer.key_id,
        created_at=now,
        retention_policy_version=retention_policy_version,
    )
    digest = canonical.checkpoint_hash(fields)
    signature = signer.sign(digest)
    await session.execute(
        insert(chain_checkpoints).values(
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
            anchor_object_key=fields.anchor_object_key,
            archive_sha256=None,
            retention_policy_version=retention_policy_version,
            created_at=now,
        )
    )
    return StoredCheckpoint(fields, digest, signature, fields.anchor_object_key)
