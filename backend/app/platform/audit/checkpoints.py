"""Checkpoints and anchors (docs/security-architecture.md §8.1, Level 2).

The hourly job, per stream, under the seal lock so no link is added meanwhile:

- **Partition-final**: once a month partition can no longer receive rows (the month's end
  plus `FINAL_MARGIN`), its remaining rows are sealed and its final checkpoint is written.
  Nothing is sealed there afterwards.
- **Daily**: once per UTC day, a checkpoint for every open partition whose month has begun
  that has none yet, or whose chain grew since its last checkpoint.

Each checkpoint is signed (the worker's key) and inserted into the stream's checkpoint chain,
then, after the commit, copied to the write-once bucket. An upload that fails is retried by
every later run (`anchor_missing`); the copy is a pure function of the stored checkpoint, so a
retry writes the same bytes. Unsealed rows older than `UNSEALED_ALERT` are logged as an
operational alert.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from app.platform.audit import canonical, chain, sealer
from app.platform.audit.canonical import CheckpointKind, Stream
from app.platform.audit.chain import StoredCheckpoint
from app.platform.audit.signing import CheckpointSigner
from app.platform.db import Database
from app.platform.storage import AnchorStore

logger = logging.getLogger(__name__)

ANCHOR_CATCH_UP: Final = timedelta(days=2)
UNSEALED_ALERT: Final = timedelta(minutes=10)


async def write_checkpoints(
    database: Database, signer: CheckpointSigner, stream: Stream, now: datetime
) -> list[StoredCheckpoint]:
    """Write the partition-final and daily checkpoints that are due; returns them."""
    created: list[StoredCheckpoint] = []
    async with database.unit_of_work() as session:
        await chain.lock(session, chain.seal_lock(stream))
        await chain.use_row_text_settings(session)
        existing = await chain.checkpoints(session, stream)
        finals = {
            item.fields.partition_key
            for item in existing
            if item.fields.kind is CheckpointKind.PARTITION_FINAL
        }
        latest: dict[str, StoredCheckpoint] = {}
        daily_today: set[str] = set()
        for item in existing:
            latest[item.fields.partition_key] = item
            if (
                item.fields.kind is CheckpointKind.DAILY
                and item.fields.created_at.astimezone(UTC).date() == now.astimezone(UTC).date()
            ):
                daily_today.add(item.fields.partition_key)
        for key in (await chain.partitions(session, stream)).keys:
            if canonical.month_start(key) > now or key in finals:
                continue
            if now >= canonical.month_end(key) + canonical.FINAL_MARGIN:
                while (
                    await sealer.seal_partition(session, stream, key, sealer.SEAL_BATCH) == sealer.SEAL_BATCH
                ):
                    pass
                end = await chain.tail(session, stream, key)
                created.append(
                    await chain.append_checkpoint(
                        session,
                        signer,
                        stream=stream,
                        kind=CheckpointKind.PARTITION_FINAL,
                        partition_key=key,
                        end=end,
                        now=now,
                    )
                )
                continue
            end = await chain.tail(session, stream, key)
            previous = latest.get(key)
            if previous is not None and (previous.fields.last_position == end.position or key in daily_today):
                continue
            created.append(
                await chain.append_checkpoint(
                    session,
                    signer,
                    stream=stream,
                    kind=CheckpointKind.DAILY,
                    partition_key=key,
                    end=end,
                    now=now,
                )
            )
    return created


async def anchor(anchors: AnchorStore, checkpoints: Sequence[StoredCheckpoint]) -> int:
    """Copy checkpoints to the write-once bucket; returns how many were written."""
    for item in checkpoints:
        await anchors.put(item.anchor_object_key, item.body)
    return len(checkpoints)


async def anchor_missing(database: Database, anchors: AnchorStore, stream: Stream, now: datetime) -> int:
    """Anchor recent checkpoints whose copy is missing (an upload that failed after commit)."""
    async with database.unit_of_work() as session:
        recent = await chain.checkpoints(session, stream, created_since=now - ANCHOR_CATCH_UP)
    missing = [item for item in recent if not (await anchors.state(item.anchor_object_key)).versions]
    return await anchor(anchors, missing)


async def unsealed_backlog(database: Database, stream: Stream, now: datetime) -> int:
    """Rows still unsealed `UNSEALED_ALERT` after they were recorded, in open partitions."""
    count = 0
    async with database.unit_of_work() as session:
        for key in await sealer.open_keys(session, stream, now):
            summary = await chain.unsealed_summary(session, stream, key, recorded_before=now - UNSEALED_ALERT)
            count += summary.count
    if count:
        logger.error("audit.unsealed_backlog", extra={"stream": stream.value, "rows": count})
    return count
