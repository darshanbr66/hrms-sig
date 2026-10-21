"""The audit sealer (docs/security-architecture.md §8.1, Level 1).

Every minute, the worker appends committed records that have no link yet to their month's
chain in `audit_chain_links`: `row_digest` over the row's canonical encoding, `link_hash`
over the previous link, the digest and the position. Chain order is sealing order. Rows are
found by their absence from `chain_links`, not by a time watermark, because a row becomes
visible only when its transaction commits, which can be after later rows' `recorded_at`.

One sealer per stream at a time (a lock document, besides the job lease). A month with a
partition-final checkpoint is never sealed again: a record appearing there is tampering, which
verification reports.
"""

from datetime import datetime
from typing import Final

from app.platform.audit import canonical, chain
from app.platform.audit.canonical import CheckpointKind, Stream
from app.platform.audit.tables import ChainCheckpoint
from app.platform.db import Database, Session

# Records sealed per transaction, and transactions per run: a backlog drains over a few runs
# without one transaction outliving MongoDB's 60-second transaction limit.
SEAL_BATCH: Final = 500
MAX_BATCHES: Final = 25


async def final_keys(session: Session, stream: Stream) -> set[str]:
    values = await session.collection(ChainCheckpoint).distinct(
        "partition_key",
        {"stream": stream.value, "kind": CheckpointKind.PARTITION_FINAL.value},
        session=session.client_session,
    )
    return {str(value) for value in values}


async def open_keys(session: Session, stream: Stream, now: datetime) -> list[str]:
    """Months that have begun, hold records and have no partition-final checkpoint."""
    finals = await final_keys(session, stream)
    return [
        key
        for key in await chain.record_months(session, stream)
        if canonical.month_start(key) <= now and key not in finals
    ]


async def seal_partition(session: Session, stream: Stream, key: str, limit: int) -> int:
    """Append up to `limit` unsealed records of one month to its chain. The caller holds the
    seal lock."""
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
    """Seal what is committed now; returns the number of rows sealed. A batch that collides with
    another holder of the stream's seal lock (a second sealer, checkpointing, retention) runs
    again once that one has committed."""

    async def batch() -> int:
        async with database.unit_of_work() as session:
            await chain.lock(session, chain.seal_lock(stream))
            sealed = 0
            for key in await open_keys(session, stream, now):
                sealed += await seal_partition(session, stream, key, SEAL_BATCH - sealed)
                if sealed >= SEAL_BATCH:
                    break
            return sealed

    total = 0
    for _ in range(MAX_BATCHES):
        sealed = await database.retry_on_conflict(batch)
        total += sealed
        if sealed < SEAL_BATCH:
            break
    return total
