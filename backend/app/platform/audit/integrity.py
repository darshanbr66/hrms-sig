"""The audit integrity jobs (docs/security-architecture.md §8.1, docs/api-architecture.md §10).

| Task | Schedule | Database user |
|---|---|---|
| `audit.seal` | every minute | worker: chain committed records |
| `audit.checkpoint` | hourly | worker: final and daily checkpoints, unsealed-record alert |
| `audit.verify_recent` | daily | worker, read-only: the checkpoint chain, open and recent months |
| `audit.verify_full` | monthly | worker, read-only: everything |
| `audit.retention` | daily | retention user: tombstones and removal, when configured |

Each runs one slot at a time (job lease); sealing and checkpointing also serialize per stream
on lock documents. A verification or retention run that finds a mismatch records a
high-severity `audit.chain_mismatch` security event and calls `on_mismatch`, which the worker
wires to the alert email for every organization's super admins and auditors.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Final

from app.platform.audit import checkpoints, retention, sealer
from app.platform.audit.canonical import Stream
from app.platform.audit.records import RequestContext, SecurityEvent, SecurityEventType, Severity
from app.platform.audit.signing import CheckpointSigner, SignatureVerifier
from app.platform.audit.verify import Report, Scope, verify_stream
from app.platform.audit.writer import AuditWriter
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.jobs import Scheduler

logger = logging.getLogger(__name__)

SCHEDULES: Final = {
    "audit.seal": "* * * * *",
    "audit.checkpoint": "7 * * * *",
    "audit.verify_recent": "30 2 * * *",
    "audit.verify_full": "40 3 1 * *",
    "audit.retention": "20 4 * * *",
}
MAX_CHECK_NAMES: Final = 400

# Called with the failed report and the time, after the security event is recorded.
type MismatchHandler = Callable[[Report, datetime], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AuditIntegrity:
    database: Database
    # The retention user's connection; None when no retention period is configured.
    retention_database: Database | None
    signer: CheckpointSigner
    verifier: SignatureVerifier
    clock: Clock
    audit: AuditWriter
    retention_months: Mapping[Stream, int | None]
    on_mismatch: MismatchHandler

    async def seal(self) -> int:
        now = self.clock.now()
        return sum([await sealer.seal(self.database, stream, now) for stream in Stream])

    async def checkpoint(self) -> int:
        now = self.clock.now()
        written = 0
        for stream in Stream:
            written += len(
                await self.database.retry_on_conflict(
                    partial(checkpoints.write_checkpoints, self.database, self.signer, stream, now)
                )
            )
            await checkpoints.unsealed_backlog(self.database, stream, now)
        return written

    async def verify(self, scope: Scope) -> list[Report]:
        now = self.clock.now()
        reports = []
        for stream in Stream:
            report = await verify_stream(self.database, self.verifier, stream=stream, scope=scope, now=now)
            if not report.ok:
                await self.report_mismatch(report, now)
            reports.append(report)
        return reports

    async def retention(self) -> list[retention.RetentionResult]:
        now = self.clock.now()
        results = []
        for stream in Stream:
            months = self.retention_months.get(stream)
            if months is None or self.retention_database is None:
                continue
            result = await retention.apply_retention(
                self.retention_database, self.signer, self.verifier, stream=stream, months=months, now=now
            )
            if result.failed_verification is not None:
                await self.report_mismatch(result.failed_verification, now)
            results.append(result)
        return results

    async def report_mismatch(self, report: Report, now: datetime) -> None:
        checks = ",".join(sorted({finding.check for finding in report.findings}))
        # A platform event: the chain covers every organization, so it belongs to none.
        async with self.database.unit_of_work() as session:
            await self.audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.AUDIT_CHAIN_MISMATCH,
                    Severity.HIGH,
                    context=RequestContext(),
                    details={
                        "stream": report.stream.value,
                        "scope": report.scope.value,
                        "findings": len(report.findings),
                        "checks": checks[:MAX_CHECK_NAMES],
                    },
                ),
            )
        await self.on_mismatch(report, now)


def register_audit_integrity(scheduler: Scheduler, integrity: AuditIntegrity) -> None:
    """Register the integrity tasks and their schedules on the worker's scheduler."""

    @scheduler.periodic("audit.seal", SCHEDULES["audit.seal"])
    async def seal() -> None:
        sealed = await integrity.seal()
        if sealed:
            logger.info("audit.sealed", extra={"rows": sealed})

    @scheduler.periodic("audit.checkpoint", SCHEDULES["audit.checkpoint"], attempts=3)
    async def checkpoint() -> None:
        logger.info("audit.checkpoints_written", extra={"checkpoints": await integrity.checkpoint()})

    @scheduler.periodic("audit.verify_recent", SCHEDULES["audit.verify_recent"])
    async def verify_recent() -> None:
        _log_reports(await integrity.verify(Scope.RECENT))

    @scheduler.periodic("audit.verify_full", SCHEDULES["audit.verify_full"])
    async def verify_full() -> None:
        _log_reports(await integrity.verify(Scope.FULL))

    @scheduler.periodic("audit.retention", SCHEDULES["audit.retention"])
    async def apply_retention() -> None:
        for result in await integrity.retention():
            logger.info(
                "audit.retention_applied",
                extra={
                    "stream": result.stream.value,
                    "removed": len(result.removed),
                    "verified": result.failed_verification is None,
                },
            )


def _log_reports(reports: list[Report]) -> None:
    for report in reports:
        logger.info(
            "audit.verified",
            extra={
                "stream": report.stream.value,
                "scope": report.scope.value,
                "ok": report.ok,
                "partitions": len(report.partitions_checked),
                "rows": report.rows_checked,
                "checkpoints": report.checkpoints_checked,
            },
        )
