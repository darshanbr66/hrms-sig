"""Producing and removing export files (worker, every minute; product-requirements RPT-2, RPT-3).

Each run (for each organization) claims queued exports one at a time with an atomic update
that marks one `running`, builds the rows from the parameters fixed when it was requested,
writes the CSV to private storage and marks it `done` with a 24-hour expiry, then tells the
requester. A failure is recorded by exception class only and the requester is told; an export
left `running` by a stopped worker is retried, up to `MAX_ATTEMPTS`. Files past their expiry
are removed from storage and the export marked `expired`.
"""

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Final

from app.modules.attendance import public as attendance
from app.modules.leave import public as leave
from app.modules.notify.public import NotificationType, notify
from app.modules.people import public as people
from app.modules.reports.audit_rows import AUDIT_HEADER, audit_rows
from app.modules.reports.models import ExportJob, ExportStatus, ExportType
from app.modules.reports.service import SPECS
from app.platform.clock import Clock
from app.platform.csvfile import Cell, to_csv
from app.platform.db import Database, Session
from app.platform.files import FileStore
from app.platform.jobs import Scheduler

logger = logging.getLogger(__name__)

TASK_NAME: Final = "reports.exports"
SCHEDULE: Final = "* * * * *"
BATCH: Final = 5
MAX_ATTEMPTS: Final = 3
STALE: Final = timedelta(minutes=15)
KEEP: Final = timedelta(hours=24)


async def _rows(session: Session, job: ExportJob, now: datetime) -> tuple[tuple[str, ...], list[list[Cell]]]:
    p = job.parameters
    start = date.fromisoformat(p["start"]) if p.get("start") else None
    end = date.fromisoformat(p["end"]) if p.get("end") else None
    ids = [uuid.UUID(value) for value in p.get("employee_ids", [])]
    match ExportType(job.export_type):
        case ExportType.ATTENDANCE_SUMMARY if start and end:
            return attendance.SUMMARY_HEADER, await attendance.summary_rows(session, ids, start, end, now)
        case ExportType.ATTENDANCE_DAYS if start and end:
            return attendance.DAYS_HEADER, await attendance.day_rows(session, ids, start, end, now)
        case ExportType.LEAVE_REQUESTS if start and end:
            return leave.REQUESTS_HEADER, await leave.request_rows(session, ids, start, end)
        case ExportType.LEAVE_BALANCES:
            return leave.BALANCES_HEADER, await leave.balance_rows(session, ids, now.date())
        case ExportType.EMPLOYEES:
            return people.EMPLOYEES_HEADER, await people.employee_rows(session, ids, now.date())
        case ExportType.AUDIT_LOG if start and end:
            return AUDIT_HEADER, await audit_rows(session, start, end)
        case _:
            raise ValueError("export parameters are incomplete")


async def _claim(database: Database, now: datetime) -> ExportJob | None:
    exhausted = {"$gte": ["$attempts", MAX_ATTEMPTS]}
    async with database.unit_of_work() as session:
        job = await session.find_one_and_update(
            ExportJob,
            {
                "$or": [
                    {"status": ExportStatus.QUEUED.value},
                    {"status": ExportStatus.RUNNING.value, "started_at": {"$lt": now - STALE}},
                ]
            },
            [
                {
                    "$set": {
                        "status": {
                            "$cond": [exhausted, ExportStatus.FAILED.value, ExportStatus.RUNNING.value]
                        },
                        "error_code": {"$cond": [exhausted, "TooManyAttempts", "$error_code"]},
                        "completed_at": {"$cond": [exhausted, now, "$completed_at"]},
                        "started_at": {"$cond": [exhausted, "$started_at", now]},
                        "attempts": {"$cond": [exhausted, "$attempts", {"$add": ["$attempts", 1]}]},
                    }
                }
            ],
            sort=[("requested_at", 1), ("id", 1)],
        )
        if job is None:
            return None
        if job.status == ExportStatus.FAILED:
            await _tell(session, job, ready=False)
            return None
        return job


async def _tell(session: Session, job: ExportJob, *, ready: bool) -> None:
    title = SPECS[ExportType(job.export_type)].title.replace("-", " ").capitalize()
    await notify(
        session,
        user_id=job.requested_by,
        kind=NotificationType.EXPORT_READY,
        title="Your export is ready" if ready else "Your export could not be produced",
        body=f"{title}: download it within 24 hours." if ready else f"{title}: ask for it again.",
        link="/reports/exports",
        idempotency_key=f"export:{job.id}:{'ready' if ready else 'failed'}",
        subject=("export_job", job.id),
    )


async def produce(database: Database, store: FileStore, clock: Clock) -> int:
    produced = 0
    for _ in range(BATCH):
        now = clock.now()
        job = await _claim(database, now)
        if job is None:
            break
        try:
            async with database.unit_of_work() as session:
                header, rows = await _rows(session, job, now)
            body, count = to_csv(header, rows)
            key = f"exports/{uuid.uuid4()}"
            await store.put(key, body, "text/csv")
        except Exception as exc:  # any failure is recorded on the export and reported
            logger.error(
                "reports.export_failed", extra={"export_id": str(job.id), "error": type(exc).__name__}
            )
            async with database.unit_of_work() as session:
                failed = await session.get(ExportJob, job.id, for_update=True)
                if failed is not None:
                    failed.status = ExportStatus.FAILED.value
                    failed.error_code = type(exc).__name__[:100]
                    failed.completed_at = clock.now()
                    await _tell(session, failed, ready=False)
            continue
        async with database.unit_of_work() as session:
            done = await session.get(ExportJob, job.id, for_update=True)
            if done is None:
                continue
            finished = clock.now()
            done.status = ExportStatus.DONE.value
            done.storage_key = key
            done.row_count = count
            done.file_name = f"{SPECS[ExportType(done.export_type)].title}-{finished.date().isoformat()}.csv"
            done.completed_at = finished
            done.expires_at = finished + KEEP
            await _tell(session, done, ready=True)
        produced += 1
    return produced


async def purge(database: Database, store: FileStore, clock: Clock) -> int:
    removed = 0
    async with database.unit_of_work() as session:
        found = await session.find(
            ExportJob, {"status": ExportStatus.DONE.value, "expires_at": {"$lte": clock.now()}}, limit=100
        )
        expired = [job for job in [await session.get(ExportJob, f.id, for_update=True) for f in found] if job]
        for job in expired:
            if job.storage_key is not None:
                await store.delete(job.storage_key)
            job.status = ExportStatus.EXPIRED.value
            job.storage_key = None
            removed += 1
    return removed


def register_exports(scheduler: Scheduler, database: Database, store: FileStore, clock: Clock) -> None:
    @scheduler.periodic(TASK_NAME, SCHEDULE, per_organization=True)
    async def run_exports() -> None:
        produced = await produce(database, store, clock)
        removed = await purge(database, store, clock)
        if produced or removed:
            logger.info("reports.exports", extra={"produced": produced, "removed": removed})
