"""Producing and removing export files (worker, every minute; product-requirements RPT-2, RPT-3).

Each run takes queued exports one at a time (`FOR UPDATE SKIP LOCKED`), marks one `running`
in its own transaction, builds the rows from the parameters fixed when it was requested,
writes the CSV to private storage and marks it `done` with a 24-hour expiry, then tells the
requester. A failure is recorded by exception class only and the requester is told; an export
left `running` by a stopped worker is retried, up to `MAX_ATTEMPTS`. Files past their expiry
are removed from storage and the export marked `expired`.
"""

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Final

import procrastinate
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance import public as attendance
from app.modules.leave import public as leave
from app.modules.notify.public import NotificationType, notify
from app.modules.people import public as people
from app.modules.reports.audit_rows import AUDIT_HEADER, audit_rows
from app.modules.reports.models import ExportJob, ExportStatus, ExportType
from app.modules.reports.service import SPECS
from app.platform.clock import Clock
from app.platform.csvfile import Cell, to_csv
from app.platform.db import Database
from app.platform.files import FileStore

logger = logging.getLogger(__name__)

TASK_NAME: Final = "reports.exports"
SCHEDULE: Final = "* * * * *"
QUEUE: Final = "exports"
BATCH: Final = 5
MAX_ATTEMPTS: Final = 3
STALE: Final = timedelta(minutes=15)
KEEP: Final = timedelta(hours=24)


async def _rows(
    session: AsyncSession, job: ExportJob, now: datetime
) -> tuple[tuple[str, ...], list[list[Cell]]]:
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
    async with database.unit_of_work() as session:
        job = (
            await session.scalars(
                select(ExportJob)
                .where(
                    or_(
                        ExportJob.status == ExportStatus.QUEUED.value,
                        (ExportJob.status == ExportStatus.RUNNING.value)
                        & (ExportJob.started_at < now - STALE),
                    )
                )
                .order_by(ExportJob.requested_at, ExportJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).first()
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status = ExportStatus.FAILED.value
            job.error_code = "TooManyAttempts"
            job.completed_at = now
            await _tell(session, job, ready=False)
            return None
        job.status = ExportStatus.RUNNING.value
        job.started_at = now
        job.attempts += 1
        return job


async def _tell(session: AsyncSession, job: ExportJob, *, ready: bool) -> None:
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
                failed = await session.get(ExportJob, job.id, with_for_update=True)
                if failed is not None:
                    failed.status = ExportStatus.FAILED.value
                    failed.error_code = type(exc).__name__[:100]
                    failed.completed_at = clock.now()
                    await _tell(session, failed, ready=False)
            continue
        async with database.unit_of_work() as session:
            done = await session.get(ExportJob, job.id, with_for_update=True)
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
        expired = list(
            await session.scalars(
                select(ExportJob)
                .where(ExportJob.status == ExportStatus.DONE.value, ExportJob.expires_at <= clock.now())
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        for job in expired:
            if job.storage_key is not None:
                await store.delete(job.storage_key)
            job.status = ExportStatus.EXPIRED.value
            job.storage_key = None
            removed += 1
    return removed


def register_exports(job_app: procrastinate.App, database: Database, store: FileStore, clock: Clock) -> None:
    @job_app.periodic(cron=SCHEDULE, periodic_id=TASK_NAME)
    @job_app.task(name=TASK_NAME, queue=QUEUE, lock=TASK_NAME)
    async def run_exports(timestamp: int) -> None:
        produced = await produce(database, store, clock)
        removed = await purge(database, store, clock)
        if produced or removed:
            logger.info("reports.exports", extra={"produced": produced, "removed": removed})
