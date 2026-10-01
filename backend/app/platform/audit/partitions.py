"""Monthly partition maintenance for the audit streams.

Audit rows land in the partition for the UTC month of their `recorded_at`. If that
partition were missing, the audit insert would fail and the business change would roll
back with it: nothing goes unrecorded, but the change is refused. This job keeps the
current month and the next MONTHS_AHEAD months in place for both streams, well ahead of
need, so a few failed hourly runs cost nothing.

`audit.ensure_partitions` (revision 0005) does the work as the table owner; the worker
role may execute it and nothing else in the audit schema beyond INSERT and SELECT.
"""

import logging
from typing import Final

import procrastinate
from procrastinate import RetryStrategy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.db import Database

logger = logging.getLogger(__name__)

MONTHS_AHEAD: Final = 3
TASK_NAME: Final = "audit.ensure_partitions"
# Hourly, off the hour so it does not coincide with other top-of-the-hour work.
SCHEDULE: Final = "17 * * * *"
QUEUE: Final = "audit"


async def ensure_partitions(session: AsyncSession, months_ahead: int = MONTHS_AHEAD) -> int:
    """Create any missing month partitions; returns how many were created."""
    result = await session.execute(text("SELECT audit.ensure_partitions(:months)"), {"months": months_ahead})
    return int(result.scalar_one())


def register_partition_maintenance(job_app: procrastinate.App, database: Database) -> None:
    """Register the hourly maintenance task on the worker's job app."""

    @job_app.periodic(cron=SCHEDULE, periodic_id=TASK_NAME)
    @job_app.task(
        name=TASK_NAME,
        queue=QUEUE,
        # Only one run at a time; the database function also serializes callers.
        lock=TASK_NAME,
        retry=RetryStrategy(max_attempts=5, exponential_wait=10),
    )
    async def ensure_audit_partitions(timestamp: int) -> None:
        async with database.unit_of_work() as session:
            created = await ensure_partitions(session)
        logger.info("audit.partitions_ensured", extra={"created": created, "months_ahead": MONTHS_AHEAD})
