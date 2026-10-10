"""The audit sealer (docs/security-architecture.md §8.1, Level 1).

Every minute, the worker appends committed rows that have no link yet to their partition's
chain in `audit.chain_links`: `row_digest` over the row's canonical encoding, `link_hash`
over the previous link, the digest and the position. Chain order is sealing order. Rows are
found by their absence from `chain_links`, not by a time watermark, because a row becomes
visible only when its transaction commits, which can be after later rows' `recorded_at`.

One sealer per stream at a time (an advisory lock, besides the job lock). A partition with a
partition-final checkpoint is never sealed again: a row appearing there is tampering, which
verification reports.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit import canonical, chain
from app.platform.audit.canonical import CheckpointKind, Stream
from app.platform.audit.tables import chain_checkpoints
from app.platform.db import Database

# Rows sealed per transaction, and transactions per run: a backlog drains over a few runs
# without one transaction outliving the worker's transaction_timeout.
SEAL_BATCH: Final = 2000
MAX_BATCHES: Final = 25


async def final_keys(session: AsyncSession, stream: Stream) -> set[str]:
    rows = await session.execute(
        select(chain_checkpoints.c.partition_key).where(
            chain_checkpoints.c.stream == stream.value,
            chain_checkpoints.c.kind == CheckpointKind.PARTITION_FINAL.value,
        )
    )
    return set(rows.scalars())


async def open_keys(session: AsyncSession, stream: Stream, now: datetime) -> list[str]:
    """Partitions whose month has begun and that have no partition-final checkpoint."""
    finals = await final_keys(session, stream)
    return [
        key
        for key in (await chain.partitions(session, stream)).keys
        if canonical.month_start(key) <= now and key not in finals
    ]


async def seal_partition(session: AsyncSession, stream: Stream, key: str, limit: int) -> int:
    """Append up to `limit` unsealed rows of one partition to its chain. The caller holds the
    seal lock and has set the row text settings."""
    if limit <= 0:
        return 0
    rows = await chain.unsealed_rows(session, stream, key, limit)
    if not rows:
        return 0
    end = await chain.tail(session, stream, key)
    position, previous = end.position, end.link_hash
    links: list[chain.NewLink] = []
    for row in rows:
        position += 1
        digest = canonical.row_digest(stream, row.values)
        previous = canonical.link_hash(previous, digest, position)
        links.append(chain.NewLink(position, row.id, digest, previous))
    await chain.insert_links(session, stream, key, links)
    return len(links)


async def seal(database: Database, stream: Stream, now: datetime) -> int:
    """Seal what is committed now; returns the number of rows sealed."""
    total = 0
    for _ in range(MAX_BATCHES):
        async with database.unit_of_work() as session:
            await chain.lock(session, chain.seal_lock(stream))
            await chain.use_row_text_settings(session)
            sealed = 0
            for key in await open_keys(session, stream, now):
                sealed += await seal_partition(session, stream, key, SEAL_BATCH - sealed)
                if sealed >= SEAL_BATCH:
                    break
        total += sealed
        if sealed < SEAL_BATCH:
            break
    return total
