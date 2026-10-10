"""Audit chain verification (docs/security-architecture.md §8.1, "Daily verification").

Read-only. For one stream:

1. **Checkpoint chain**: numbers run 1, 2, 3 … without gaps; each names the previous one's
   hash; each hash is recomputed from what the checkpoint states; each signature verifies
   with the key its key ID names.
2. **Anchors**: every checkpoint in scope has a write-once copy, every version of which equals
   the stored checkpoint, under compliance-mode retention; no copy exists without its row.
3. **Coverage**: every partition named by a checkpoint or a chain partition is attached or
   has a retention tombstone, which matches the partition's final checkpoint; every month
   that ended long enough ago has a partition-final checkpoint; no unexpected table is
   attached to the stream.
4. **Rows**: for each partition in scope, every link's row is recomputed and re-chained from
   the genesis; every checkpoint's position exists and carries its link hash; nothing follows
   a partition-final checkpoint; open partitions have no rows left unsealed for long.

`Scope.RECENT` (daily) checks the whole checkpoint chain but only recent anchors and the open
or recently closed partitions; `Scope.FULL` (monthly, and before retention removes anything)
checks everything. Findings name positions, counts and partitions, never row contents.
"""

import logging
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit import canonical, chain, sealer
from app.platform.audit.canonical import CheckpointKind, Stream
from app.platform.audit.chain import StoredCheckpoint
from app.platform.audit.signing import SignatureVerifier
from app.platform.db import Database
from app.platform.storage import AnchorStore, AnchorStoreError

logger = logging.getLogger(__name__)

RECENT: Final = timedelta(days=3)
# A checkpoint's copy may lag its commit until the next hourly run retries the upload.
ANCHOR_GRACE: Final = timedelta(hours=2)
# A month without its final checkpoint this long after it closed means the job is not running.
FINAL_GRACE: Final = timedelta(days=1)
UNSEALED_ALERT: Final = timedelta(minutes=10)
BATCH: Final = 5000
COMPLIANCE: Final = "COMPLIANCE"


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
    anchors_checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, check: str, partition_key: str | None, detail: str) -> None:
        self.findings.append(Finding(check, partition_key, detail))


async def verify_stream(
    database: Database,
    anchors: AnchorStore,
    verifier: SignatureVerifier,
    *,
    stream: Stream,
    scope: Scope,
    now: datetime,
    only_partitions: Collection[str] | None = None,
) -> Report:
    """Verify one stream. `only_partitions` limits the row checks (retention verifies the
    partition it is about to remove); everything else follows `scope`."""
    report = Report(stream, scope)
    async with database.unit_of_work() as session:
        stored = await chain.checkpoints(session, stream)
        attached = await chain.partitions(session, stream)
        chain_keys = await chain.chain_partition_keys(session, stream)
    _check_checkpoint_chain(report, verifier, stored)
    await _check_anchors(report, anchors, stream=stream, stored=stored, scope=scope, now=now)

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
        report, attached, chain_keys=chain_keys, by_key=by_key, finals=finals, tombstones=tombstones, now=now
    )

    started = [key for key in attached.keys if canonical.month_start(key) <= now]
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
        if item.anchor_object_key != fields.anchor_object_key:
            report.add("checkpoint_anchor_key", fields.partition_key, f"checkpoint {fields.checkpoint_no}")
        expected_no, previous = fields.checkpoint_no + 1, item.checkpoint_hash


async def _check_anchors(
    report: Report,
    anchors: AnchorStore,
    *,
    stream: Stream,
    stored: list[StoredCheckpoint],
    scope: Scope,
    now: datetime,
) -> None:
    in_scope = (
        stored if scope is Scope.FULL else [item for item in stored if item.fields.created_at >= now - RECENT]
    )
    try:
        for item in in_scope:
            report.anchors_checked += 1
            fields, state = item.fields, await anchors.state(item.fields.anchor_object_key)
            if not state.versions:
                if fields.created_at < now - ANCHOR_GRACE:
                    report.add("anchor_missing", fields.partition_key, f"checkpoint {fields.checkpoint_no}")
                continue
            expected = item.body
            if any(version.body != expected for version in state.versions):
                report.add("anchor_mismatch", fields.partition_key, f"checkpoint {fields.checkpoint_no}")
            if any(
                version.retention_mode != COMPLIANCE
                or version.retain_until is None
                or version.retain_until <= now
                for version in state.versions
            ):
                report.add("anchor_not_locked", fields.partition_key, f"checkpoint {fields.checkpoint_no}")
            if state.delete_markers:
                report.add(
                    "anchor_delete_attempted", fields.partition_key, f"checkpoint {fields.checkpoint_no}"
                )
        known = {canonical.anchor_object_key(stream, item.fields.checkpoint_no) for item in stored}
        prefix = f"{stream.value}/"
        if scope is Scope.FULL:
            extra = [key for key in await anchors.keys(prefix) if key not in known]
        else:
            last = canonical.anchor_object_key(stream, stored[-1].fields.checkpoint_no) if stored else None
            extra = await anchors.keys(prefix, start_after=last)
        for key in extra:
            report.add("anchor_without_checkpoint", None, key)
    except AnchorStoreError as exc:
        report.add("anchor_store_unavailable", None, str(exc))


def _check_coverage(
    report: Report,
    attached: chain.Partitions,
    *,
    chain_keys: list[str],
    by_key: dict[str, list[StoredCheckpoint]],
    finals: dict[str, StoredCheckpoint],
    tombstones: dict[str, StoredCheckpoint],
    now: datetime,
) -> None:
    for name in attached.unexpected:
        report.add("unexpected_partition", None, name)
    present = set(attached.keys)
    for key in sorted(by_key):
        if key not in present and key not in tombstones:
            report.add(
                "partition_missing", key, "checkpoints name it, it is not attached and has no tombstone"
            )
    # A month dropped before any checkpoint named it still left its chain partition behind.
    for key in chain_keys:
        if key not in present and key not in tombstones and key not in by_key:
            report.add(
                "partition_missing", key, "its chain links exist, it is not attached and has no tombstone"
            )
    for key, tombstone in sorted(tombstones.items()):
        final = finals.get(key)
        if final is None or (tombstone.fields.last_position, tombstone.fields.last_link_hash) != (
            final.fields.last_position,
            final.fields.last_link_hash,
        ):
            report.add("tombstone_mismatch", key, f"checkpoint {tombstone.fields.checkpoint_no}")
    for key in attached.keys:
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
        await chain.use_row_text_settings(session)
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
    report: Report, session: AsyncSession, *, stream: Stream, key: str, needed: set[int]
) -> tuple[int, dict[int, bytes]]:
    """Re-chain the partition from its genesis; returns the last position and the link hash at
    each position in `needed`."""
    hashes_at: dict[int, bytes] = {}
    expected, previous, last_seen = 1, canonical.genesis(stream, key), 0
    while links := await chain.links_with_rows(session, stream, key, after=last_seen, limit=BATCH):
        for link in links:
            if link.position == last_seen:
                report.add("duplicate_record", key, f"position {link.position}")
                continue
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
