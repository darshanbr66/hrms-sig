"""Audit chain verification (docs/security-architecture.md §8.1, "Daily verification").

Read-only. For one stream:

1. **Checkpoint chain**: numbers run 1, 2, 3 … without gaps; each names the previous one's
   hash; each hash is recomputed from what the checkpoint states; each signature verifies
   with the key its key ID names.
2. **Coverage**: every month named by a checkpoint or by chain links still holds its records
   or has a retention tombstone, which matches the month's final checkpoint; every month that
   ended long enough ago has a partition-final checkpoint.
3. **Records**: for each month in scope, every link's record is recomputed and re-chained from
   the genesis; every checkpoint's position exists and carries its link hash; nothing follows
   a partition-final checkpoint; open months have no records left unsealed for long.

`Scope.RECENT` (daily) checks the whole checkpoint chain but only open or recently closed
months; `Scope.FULL` (monthly, and before retention removes anything) checks everything.
Findings name positions, counts and months, never record contents.

What this cannot detect without copies kept outside the database: someone with database
administrator access removing the newest records together with their links and checkpoints,
or replacing the whole chain while also holding the worker's signing key. Exported
checkpoints (`audit-export-checkpoints`) compared with the database close the first gap.
"""

import logging
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from app.platform.audit import canonical, chain, sealer
from app.platform.audit.canonical import CheckpointKind, Stream
from app.platform.audit.chain import StoredCheckpoint
from app.platform.audit.signing import SignatureVerifier
from app.platform.db import Database, Session

logger = logging.getLogger(__name__)

RECENT: Final = timedelta(days=3)
# A month without its final checkpoint this long after it closed means the job is not running.
FINAL_GRACE: Final = timedelta(days=1)
UNSEALED_ALERT: Final = timedelta(minutes=10)
BATCH: Final = 2000


class Scope(StrEnum):
    RECENT = "recent"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    partition_key: str | None
    detail: str


@dataclass(slots=True)
class Report:
    stream: Stream
    scope: Scope
    partitions_checked: list[str] = field(default_factory=list)
    rows_checked: int = 0
    checkpoints_checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, check: str, partition_key: str | None, detail: str) -> None:
        self.findings.append(Finding(check, partition_key, detail))


async def verify_stream(
    database: Database,
    verifier: SignatureVerifier,
    *,
    stream: Stream,
    scope: Scope,
    now: datetime,
    only_partitions: Collection[str] | None = None,
) -> Report:
    """Verify one stream. `only_partitions` limits the record checks (retention verifies the
    month it is about to remove); everything else follows `scope`."""
    report = Report(stream, scope)
    async with database.unit_of_work() as session:
        stored = await chain.checkpoints(session, stream)
        months = await chain.record_months(session, stream)
        link_months = await chain.link_months(session, stream)
    _check_checkpoint_chain(report, verifier, stored)

    by_key: dict[str, list[StoredCheckpoint]] = defaultdict(list)
    finals: dict[str, StoredCheckpoint] = {}
    tombstones: dict[str, StoredCheckpoint] = {}
    for item in stored:
        by_key[item.fields.partition_key].append(item)
        if item.fields.kind is CheckpointKind.PARTITION_FINAL:
            finals[item.fields.partition_key] = item
        elif item.fields.kind is CheckpointKind.RETENTION_TOMBSTONE:
            tombstones[item.fields.partition_key] = item
    _check_coverage(
        report, months, link_months=link_months, by_key=by_key, finals=finals, tombstones=tombstones, now=now
    )

    present = sorted((set(months) | set(link_months)) - set(tombstones))
    started = [key for key in present if canonical.month_start(key) <= now]
    if only_partitions is not None:
        selected = [key for key in started if key in only_partitions]
    elif scope is Scope.FULL:
        selected = started
    else:
        selected = [
            key for key in started if key not in finals or finals[key].fields.created_at >= now - RECENT
        ]
    for key in selected:
        await _check_partition(
            report, database, stream=stream, key=key, checkpoints=by_key[key], final=finals.get(key), now=now
        )
        report.partitions_checked.append(key)
    if report.findings:
        logger.error(
            "audit.verification_failed",
            extra={
                "stream": stream.value,
                "scope": scope.value,
                "findings": len(report.findings),
                "checks": sorted({finding.check for finding in report.findings}),
            },
        )
    return report


async def verify_checkpoints(database: Database, verifier: SignatureVerifier, stream: Stream) -> Report:
    """Verify only a stream's checkpoint chain (numbering, links, hashes, signatures): what the
    witness checks before it vouches for the newest checkpoint. Read-only."""
    report = Report(stream, Scope.RECENT)
    async with database.unit_of_work() as session:
        stored = await chain.checkpoints(session, stream)
    _check_checkpoint_chain(report, verifier, stored)
    return report


def _check_checkpoint_chain(
    report: Report, verifier: SignatureVerifier, stored: list[StoredCheckpoint]
) -> None:
    expected_no, previous = 1, canonical.ZERO_HASH
    for item in stored:
        fields = item.fields
        report.checkpoints_checked += 1
        if fields.checkpoint_no != expected_no:
            report.add(
                "checkpoint_gap",
                fields.partition_key,
                f"expected checkpoint {expected_no}, found {fields.checkpoint_no}",
            )
        if fields.prev_checkpoint_hash != previous:
            report.add("checkpoint_chain_broken", fields.partition_key, f"checkpoint {fields.checkpoint_no}")
        recomputed = canonical.checkpoint_hash(fields)
        if recomputed != item.checkpoint_hash:
            report.add("checkpoint_hash_mismatch", fields.partition_key, f"checkpoint {fields.checkpoint_no}")
        if not verifier.verify(fields.key_id, recomputed, item.signature):
            report.add(
                "checkpoint_signature_invalid", fields.partition_key, f"checkpoint {fields.checkpoint_no}"
            )
        expected_no, previous = fields.checkpoint_no + 1, item.checkpoint_hash


def _check_coverage(
    report: Report,
    months: list[str],
    *,
    link_months: list[str],
    by_key: dict[str, list[StoredCheckpoint]],
    finals: dict[str, StoredCheckpoint],
    tombstones: dict[str, StoredCheckpoint],
    now: datetime,
) -> None:
    present = set(months)
    for key in sorted(by_key):
        has_rows = any(item.fields.last_position > 0 for item in by_key[key])
        if has_rows and key not in present and key not in tombstones:
            report.add(
                "partition_missing",
                key,
                "checkpoints name its records, none remain and there is no tombstone",
            )
    # A month removed before any checkpoint named it still left its chain links behind.
    for key in link_months:
        if key not in present and key not in tombstones and key not in by_key:
            report.add(
                "partition_missing",
                key,
                "its chain links exist, its records do not, and there is no tombstone",
            )
    for key, tombstone in sorted(tombstones.items()):
        final = finals.get(key)
        if final is None or (tombstone.fields.last_position, tombstone.fields.last_link_hash) != (
            final.fields.last_position,
            final.fields.last_link_hash,
        ):
            report.add("tombstone_mismatch", key, f"checkpoint {tombstone.fields.checkpoint_no}")
    for key in months:
        if key not in finals and canonical.month_end(key) + canonical.FINAL_MARGIN + FINAL_GRACE <= now:
            report.add("partition_not_final", key, "the month closed without a partition-final checkpoint")


async def _check_partition(
    report: Report,
    database: Database,
    *,
    stream: Stream,
    key: str,
    checkpoints: list[StoredCheckpoint],
    final: StoredCheckpoint | None,
    now: datetime,
) -> None:
    genesis = canonical.genesis(stream, key)
    async with database.unit_of_work() as session:
        end, hashes_at = await _scan_links(
            report,
            session,
            stream=stream,
            key=key,
            needed={item.fields.last_position for item in checkpoints},
        )
        _check_partition_checkpoints(
            report, key=key, genesis=genesis, checkpoints=checkpoints, hashes_at=hashes_at
        )
        if final is not None:
            if end > final.fields.last_position:
                report.add("links_after_final", key, f"{end - final.fields.last_position} links")
            unsealed = await chain.unsealed_summary(session, stream, key)
            if unsealed.count:
                report.add("rows_after_final", key, f"{unsealed.count} rows")
        elif key in await sealer.open_keys(session, stream, now):
            unsealed = await chain.unsealed_summary(
                session, stream, key, recorded_before=now - UNSEALED_ALERT
            )
            if unsealed.count:
                report.add("unsealed_rows", key, f"{unsealed.count} rows unsealed for over 10 minutes")


async def _scan_links(
    report: Report, session: Session, *, stream: Stream, key: str, needed: set[int]
) -> tuple[int, dict[int, bytes]]:
    """Re-chain the month from its genesis; returns the last position and the link hash at
    each position in `needed`."""
    hashes_at: dict[int, bytes] = {}
    expected, previous, last_seen = 1, canonical.genesis(stream, key), 0
    while links := await chain.links_with_rows(session, stream, key, after=last_seen, limit=BATCH):
        for link in links:
            if link.position != expected:
                report.add("link_gap", key, f"positions {expected} to {link.position - 1} are missing")
            report.rows_checked += 1
            _check_link(report, stream=stream, key=key, link=link, previous=previous)
            if link.position in needed:
                hashes_at[link.position] = link.link_hash
            # Continue from the stored link, so one break is reported once.
            previous, expected, last_seen = link.link_hash, link.position + 1, link.position
    return last_seen, hashes_at


def _check_link(report: Report, *, stream: Stream, key: str, link: chain.StoredLink, previous: bytes) -> None:
    where = f"position {link.position}"
    if link.row is None:
        report.add("row_missing", key, where)
    else:
        try:
            if canonical.row_digest(stream, link.row, link.digest_version) != link.row_digest:
                report.add("row_modified", key, where)
        except ValueError:
            report.add("digest_version_unknown", key, where)
    if canonical.link_hash(previous, link.row_digest, link.position) != link.link_hash:
        report.add("link_broken", key, where)


def _check_partition_checkpoints(
    report: Report,
    *,
    key: str,
    genesis: bytes,
    checkpoints: list[StoredCheckpoint],
    hashes_at: dict[int, bytes],
) -> None:
    for item in checkpoints:
        number, position = item.fields.checkpoint_no, item.fields.last_position
        if position == 0:
            if item.fields.last_link_hash != genesis:
                report.add("checkpoint_link_mismatch", key, f"checkpoint {number}")
        elif position not in hashes_at:
            report.add("checkpoint_beyond_chain", key, f"checkpoint {number} names position {position}")
        elif hashes_at[position] != item.fields.last_link_hash:
            report.add("checkpoint_link_mismatch", key, f"checkpoint {number}")
