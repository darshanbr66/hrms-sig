"""Scanning uploaded document versions (worker, every minute; product-requirements DOC-5).

Each run takes waiting versions one at a time (`FOR UPDATE SKIP LOCKED`, so several workers
never scan the same file), reads the file from private storage, and sends it to ClamAV:

- clean: the version becomes downloadable;
- infected: it never will be; the result is audited (the signature name stays in the audit
  record and the log, never in what users see);
- the scanner or storage unavailable: the attempt is counted and retried on the next run;
  after `MAX_ATTEMPTS` the version is marked `failed`, which is also never downloadable.

Without a scanner configured nothing is scanned and versions stay waiting (fail closed).
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Final

import procrastinate
from sqlalchemy import select

from app.modules.documents.models import DocumentVersion, ScanStatus
from app.platform.audit.records import AuditActor, AuditEvent, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.files import FileStore, FileStoreError
from app.platform.scanning import ScanError, Scanner, Verdict

logger = logging.getLogger(__name__)

TASK_NAME: Final = "documents.scan"
SCHEDULE: Final = "* * * * *"
QUEUE: Final = "documents"
BATCH: Final = 20
MAX_ATTEMPTS: Final = 5


@dataclass(frozen=True, slots=True)
class ScanRun:
    clean: int = 0
    infected: int = 0
    retried: int = 0
    failed: int = 0


async def scan_pending(
    database: Database, store: FileStore, scanner: Scanner, clock: Clock, audit: AuditWriter
) -> ScanRun:
    counts = {"clean": 0, "infected": 0, "retried": 0, "failed": 0}
    tried: list[uuid.UUID] = []
    for _ in range(BATCH):
        async with database.unit_of_work() as session:
            version = (
                await session.scalars(
                    select(DocumentVersion)
                    .where(
                        DocumentVersion.scan_status == ScanStatus.PENDING.value,
                        DocumentVersion.id.not_in(tried),
                    )
                    .order_by(DocumentVersion.uploaded_at, DocumentVersion.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if version is None:
                break
            tried.append(version.id)
            try:
                result = await scanner.scan(await store.get(version.storage_key))
            except (ScanError, FileStoreError) as exc:
                version.scan_attempts += 1
                if version.scan_attempts >= MAX_ATTEMPTS:
                    version.scan_status = ScanStatus.FAILED.value
                    version.scanned_at = clock.now()
                    counts["failed"] += 1
                    logger.error(
                        "documents.scan_failed", extra={"version_id": str(version.id), "error": str(exc)}
                    )
                else:
                    counts["retried"] += 1
                await session.flush()
                if isinstance(exc, ScanError):
                    # Leave the rest for the next run rather than hammer an unavailable scanner.
                    break
                # One unreadable file does not hold up the others.
                continue
            version.scan_status = (
                ScanStatus.CLEAN if result.verdict is Verdict.CLEAN else ScanStatus.INFECTED
            ).value
            version.scan_attempts += 1
            version.scanned_at = clock.now()
            counts[version.scan_status] += 1
            if result.verdict is Verdict.INFECTED:
                logger.warning(
                    "documents.infected", extra={"version_id": str(version.id), "signature": result.signature}
                )
                await audit.record(
                    session,
                    AuditEvent(
                        action="document.scan.infected",
                        actor=AuditActor.job(),
                        target_type="document_version",
                        target_id=version.id,
                        changes={"signature": ValueChange(None, result.signature)},
                    ),
                )
    return ScanRun(**counts)


def register_document_scan(
    job_app: procrastinate.App,
    database: Database,
    *,
    store: FileStore,
    scanner: Scanner,
    clock: Clock,
    audit: AuditWriter,
) -> None:
    @job_app.periodic(cron=SCHEDULE, periodic_id=TASK_NAME)
    @job_app.task(name=TASK_NAME, queue=QUEUE, lock=TASK_NAME)
    async def scan_documents(timestamp: int) -> None:
        run = await scan_pending(database, store, scanner, clock, audit)
        if run != ScanRun():
            logger.info(
                "documents.scanned",
                extra={
                    "clean": run.clean,
                    "infected": run.infected,
                    "retried": run.retried,
                    "failed": run.failed,
                },
            )
