"""The audit integrity jobs (docs/security-architecture.md §8.1, docs/api-architecture.md §10).

| Task | Schedule | Role |
|---|---|---|
| `audit.seal` | every minute | worker: chain committed rows |
| `audit.checkpoint` | hourly | worker: final and daily checkpoints, anchors, unsealed-row alert |
| `audit.verify_recent` | daily | worker, read-only: the checkpoint chain, recent anchors and partitions |
| `audit.verify_full` | monthly | worker, read-only: everything |
| `audit.retention` | daily | `hrms_audit_retention`: tombstones and partition removal, when configured |

Each runs one at a time (job lock); sealing and checkpointing also serialize per stream in
the database. A verification or retention run that finds a mismatch records a high-severity
`audit.chain_mismatch` security event and calls `on_mismatch` in the same transaction, which
the worker wires to the alert email for super admins and auditors.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import procrastinate
from procrastinate import RetryStrategy
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit import checkpoints, retention, sealer
from app.platform.audit.canonical import Stream
from app.platform.audit.records import RequestContext, SecurityEvent, SecurityEventType, Severity
from app.platform.audit.signing import CheckpointSigner, SignatureVerifier
from app.platform.audit.verify import Report, Scope, verify_stream
from app.platform.audit.writer import AuditWriter
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.storage import AnchorStore

logger = logging.getLogger(__name__)

QUEUE: Final = "audit"
SCHEDULES: Final = {
    "audit.seal": "* * * * *",
    "audit.checkpoint": "7 * * * *",
    "audit.verify_recent": "30 2 * * *",
    "audit.verify_full": "40 3 1 * *",
    "audit.retention": "20 4 * * *",
}
MAX_CHECK_NAMES: Final = 400

# Called with the open transaction, the failed report and the time, after the security event.
type MismatchHandler = Callable[[AsyncSession, Report, datetime], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AuditIntegrity:
    database: Database
    retention_database: Database
    anchors: AnchorStore
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
            created = await checkpoints.write_checkpoints(self.database, self.signer, stream, now)
            written += len(created)
            # After the commit: a failed upload is retried by `anchor_missing` on a later run.
            try:
                await checkpoints.anchor(self.anchors, created)
            finally:
                await checkpoints.anchor_missing(self.database, self.anchors, stream, now)
            await checkpoints.unsealed_backlog(self.database, stream, now)
        return written

    async def verify(self, scope: Scope) -> list[Report]:
        now = self.clock.now()
        reports = []
        for stream in Stream:
            report = await verify_stream(
                self.database, self.anchors, self.verifier, stream=stream, scope=scope, now=now
            )
            if not report.ok:
                await self.report_mismatch(report, now)
            reports.append(report)
        return reports

    async def retention(self) -> list[retention.RetentionResult]:
        now = self.clock.now()
        results = []
        for stream in Stream:
            months = self.retention_months.get(stream)
            if months is None:
                continue
            result = await retention.apply_retention(
                self.retention_database,
                self.anchors,
                self.signer,
                self.verifier,
                stream=stream,
                months=months,
                now=now,
            )
            if result.failed_verification is not None:
                await self.report_mismatch(result.failed_verification, now)
            results.append(result)
        return results

    async def report_mismatch(self, report: Report, now: datetime) -> None:
        checks = ",".join(sorted({finding.check for finding in report.findings}))
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
            await self.on_mismatch(session, report, now)


def register_audit_integrity(job_app: procrastinate.App, integrity: AuditIntegrity) -> None:
    """Register the integrity tasks and their schedules on the worker's job app."""

    def periodic(
        name: str, retry: RetryStrategy | bool = False
    ) -> Callable[[Callable[..., Awaitable[None]]], object]:
        def register(function: Callable[..., Awaitable[None]]) -> object:
            task = job_app.task(name=name, queue=QUEUE, lock=name, retry=retry)(function)
            return job_app.periodic(cron=SCHEDULES[name], periodic_id=name)(task)

        return register

    @periodic("audit.seal")
    async def seal(timestamp: int) -> None:
        sealed = await integrity.seal()
        if sealed:
            logger.info("audit.sealed", extra={"rows": sealed})

    @periodic("audit.checkpoint", RetryStrategy(max_attempts=3, exponential_wait=30))
    async def checkpoint(timestamp: int) -> None:
        logger.info("audit.checkpoints_written", extra={"checkpoints": await integrity.checkpoint()})

    @periodic("audit.verify_recent")
    async def verify_recent(timestamp: int) -> None:
        _log_reports(await integrity.verify(Scope.RECENT))

    @periodic("audit.verify_full")
    async def verify_full(timestamp: int) -> None:
        _log_reports(await integrity.verify(Scope.FULL))

    @periodic("audit.retention")
    async def apply_retention(timestamp: int) -> None:
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
                "anchors": report.anchors_checked,
            },
        )
