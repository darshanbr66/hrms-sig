"""Removing the stored files of an erasure (owner decision 2026-10-23, docs/database-design.md
§9.1). Runs in the worker, whose database user alone may remove stored files.

Every minute, per organization: for each erasure whose files are pending, each listed file is
removed (removing a file already gone succeeds, so a retry after a partial run is safe), and
the request is updated under its lock: the removed keys leave the list, the attempt and any
storage error are recorded, and only when no key is left does the request become complete,
with its files category reported as deleted. A failure leaves the rest for the next run; the
request keeps saying the files are pending, so it is never reported complete too early.
"""

import logging
from typing import Final

from app.modules.erasure.models import ErasureRequest, ErasureStatus
from app.platform.audit.records import AuditActor, AuditEvent, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.erasure import Outcome
from app.platform.files import FileStore, FileStoreError
from app.platform.jobs import Scheduler

logger = logging.getLogger(__name__)

TASK_NAME: Final = "erasure.remove_files"
SCHEDULE: Final = "* * * * *"
BATCH: Final = 20
FILES_CATEGORY: Final = "documents.files"


async def remove_files(database: Database, store: FileStore, clock: Clock, audit: AuditWriter) -> int:
    """Remove pending files; returns how many requests became complete."""
    completed = 0
    async with database.unit_of_work() as session:
        due = await session.find(
            ErasureRequest,
            {"status": ErasureStatus.FILES_PENDING.value},
            sort=[("requested_at", 1)],
            limit=BATCH,
        )
    for found in due:
        removed: list[str] = []
        error: str | None = None
        for key in found.pending_storage_keys:
            try:
                await store.delete(key)
            except FileStoreError as exc:
                error = type(exc).__name__
                logger.warning(
                    "erasure.file_removal_failed", extra={"erasure_id": str(found.id), "error": error}
                )
                break
            removed.append(key)
        async with database.unit_of_work() as session:
            request = await session.get(ErasureRequest, found.id, for_update=True)
            if request is None or request.status != ErasureStatus.FILES_PENDING:
                continue
            now = clock.now()
            gone = set(removed)
            request.pending_storage_keys = [key for key in request.pending_storage_keys if key not in gone]
            request.files_removed += len(gone)
            request.file_attempts += 1
            request.last_file_error = error
            request.updated_at = now
            request.version += 1
            if request.pending_storage_keys:
                continue
            request.status = ErasureStatus.COMPLETED.value
            request.open = False
            request.completed_at = now
            request.result = [
                {**category, "outcome": Outcome.DELETED.value}
                if category["key"] == FILES_CATEGORY
                else category
                for category in request.result or []
            ]
            await audit.record(
                session,
                AuditEvent(
                    action="people.erasure.completed",
                    actor=AuditActor.job(),
                    target_type="employee_erasure",
                    target_id=request.id,
                    subject_employee_id=request.employee_id,
                    changes={
                        "status": ValueChange(
                            ErasureStatus.FILES_PENDING.value, ErasureStatus.COMPLETED.value
                        ),
                        "files_removed": ValueChange(None, request.files_removed),
                    },
                ),
            )
            completed += 1
    return completed


def register_erasure_files(
    scheduler: Scheduler, database: Database, store: FileStore, clock: Clock, audit: AuditWriter
) -> None:
    @scheduler.periodic(TASK_NAME, SCHEDULE, per_organization=True)
    async def run_erasure_files() -> None:
        done = await remove_files(database, store, clock, audit)
        if done:
            logger.info("erasure.completed", extra={"requests": done})
