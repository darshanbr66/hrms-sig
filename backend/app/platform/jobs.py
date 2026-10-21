"""The worker's scheduled tasks on MongoDB (docs/api-architecture.md §10, ADR-030).

Every task the system runs in the background is periodic: send the email outbox, seal the
audit chain, expire grants, accrue leave, build exports, scan uploads. Each has a cron
schedule (minute, hour, day of month, month, day of week; `*`, `*/n`, `a-b`, `a,b`, UTC).

- **One run per slot.** A task's state is one document in `job_leases`. A worker claims the
  latest due slot with an atomic update that succeeds only when the lease is free and the slot
  is newer than the last one run, so two workers never run the same slot, and a slot missed
  while no worker ran is run once, not once per missed minute.
- **Recovery.** A claim is a lease (`LEASE`). A worker that stops mid-run leaves the lease to
  expire; the slot is then claimed again. Tasks are written to be idempotent, and the units
  they process (outbox messages, exports, scans) are claimed one by one with their own state.
- **Retries.** A failed run is retried up to the task's `attempts`, then recorded as failed
  (`last_error_at`, `failures`) and left for the next slot; the error is logged without data.
- **Organizations.** A `per_organization` task runs once for every active organization, each
  inside that organization's tenant scope; a failure in one does not stop the others.
"""

import asyncio
import contextlib
import logging
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.platform.clock import Clock
from app.platform.db import ID, Database, tenant_scope, utcnow
from app.platform.tenancy import Organization, OrganizationStatus

logger = logging.getLogger(__name__)

LEASES_COLLECTION: Final = "job_leases"
LEASE: Final = timedelta(minutes=15)
TICK_SECONDS: Final = 15
# How far back a slot is searched for (the longest schedule is monthly).
MAX_LOOKBACK: Final = timedelta(days=32)
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
# A scheduler that has not ticked or finished a task for this long is not running: a task that
# outlives its lease is already treated as dead by every other host.
HEARTBEAT_STALE: Final = LEASE + timedelta(seconds=4 * TICK_SECONDS)

type TaskFunction = Callable[[], Awaitable[None]]


class JobHealth:
    """Whether this process's scheduler is alive, for readiness (`/api/health/ready`).

    The scheduler beats on every tick and after every task. The host marks it failed when the
    scheduler stops for good. Healthy means: not failed, and a beat within `HEARTBEAT_STALE`
    (the host's start counts as the first beat)."""

    def __init__(
        self, *, stale_after: timedelta = HEARTBEAT_STALE, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._stale_after = stale_after.total_seconds()
        self._monotonic = monotonic
        self._last_beat: float | None = None
        self._failure: str | None = None

    def beat(self) -> None:
        self._last_beat = self._monotonic()

    def fail(self, reason: str) -> None:
        self._failure = reason

    @property
    def failure(self) -> str | None:
        return self._failure

    def healthy(self) -> bool:
        if self._failure is not None or self._last_beat is None:
            return False
        return self._monotonic() - self._last_beat <= self._stale_after


# --- cron ---------------------------------------------------------------------------------------


def _field(spec: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for item in spec.split(","):
        part, step = item, 1
        if "/" in item:
            part, step_text = item.split("/", 1)
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            first, last = part.split("-", 1)
            start, end = int(first), int(last)
        else:
            start = end = int(part)
        if not low <= start <= end <= high or step < 1:
            raise ValueError(f"cron field {spec!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]

    @classmethod
    def parse(cls, spec: str) -> "Cron":
        parts = spec.split()
        if len(parts) != 5:
            raise ValueError(f"cron {spec!r} needs five fields")
        weekdays = _field(parts[4], 0, 7)
        return cls(
            _field(parts[0], 0, 59),
            _field(parts[1], 0, 23),
            _field(parts[2], 1, 31),
            _field(parts[3], 1, 12),
            frozenset(7 if day == 0 else day for day in weekdays),
        )

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.day in self.days
            and moment.month in self.months
            and moment.isoweekday() in self.weekdays
        )

    def latest(self, now: datetime) -> datetime | None:
        """The most recent scheduled minute at or before `now`."""
        moment = now.astimezone(UTC).replace(second=0, microsecond=0)
        floor = moment - MAX_LOOKBACK
        while moment >= floor:
            if (
                moment.month in self.months
                and moment.day in self.days
                and moment.isoweekday() in self.weekdays
            ):
                if moment.hour in self.hours:
                    if moment.minute in self.minutes:
                        return moment
                    moment -= timedelta(minutes=1)
                    continue
                moment = moment.replace(minute=59) - timedelta(hours=1)
                continue
            moment = moment.replace(hour=23, minute=59) - timedelta(days=1)
        return None


# --- tasks --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodicTask:
    name: str
    cron: str
    run: TaskFunction
    attempts: int = 1
    per_organization: bool = False
    schedule: Cron = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schedule", Cron.parse(self.cron))
        if self.attempts < 1:
            raise ValueError("a task runs at least once")


class Scheduler:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        *,
        owner: str | None = None,
        health: JobHealth | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._leases = database.collection(LEASES_COLLECTION)
        self._owner = owner or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._tasks: dict[str, PeriodicTask] = {}
        self._health = health

    def register(self, task: PeriodicTask) -> None:
        if task.name in self._tasks:
            raise ValueError(f"task {task.name} is registered twice")
        self._tasks[task.name] = task

    def periodic(
        self, name: str, cron: str, *, attempts: int = 1, per_organization: bool = False
    ) -> Callable[[TaskFunction], TaskFunction]:
        def register(function: TaskFunction) -> TaskFunction:
            self.register(PeriodicTask(name, cron, function, attempts, per_organization))
            return function

        return register

    @property
    def task_names(self) -> list[str]:
        return sorted(self._tasks)

    async def run_due(self, stop: asyncio.Event | None = None) -> list[str]:
        """Run every task whose latest slot has not run yet; returns the names run. Once `stop`
        is set, no further task is started."""
        ran: list[str] = []
        for task in self._tasks.values():
            if stop is not None and stop.is_set():
                break
            slot = task.schedule.latest(self._clock.now())
            if slot is None or not await self._claim(task, slot):
                continue
            await self._execute(task, slot)
            self._beat()
            ran.append(task.name)
        return ran

    def _beat(self) -> None:
        if self._health is not None:
            self._health.beat()

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("jobs.worker_started", extra={"tasks": self.task_names})
        while not stop.is_set():
            self._beat()
            try:
                await self.run_due(stop)
            except PyMongoError as exc:
                logger.warning("jobs.database_unavailable", extra={"error_type": type(exc).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        logger.info("jobs.worker_stopped")

    async def _claim(self, task: PeriodicTask, slot: datetime) -> bool:
        now = utcnow()
        await self._leases.update_one(
            {ID: task.name},
            {"$setOnInsert": {"owner": None, "lease_until": EPOCH, "last_slot": EPOCH, "failures": 0}},
            upsert=True,
        )
        claimed = await self._leases.find_one_and_update(
            {
                ID: task.name,
                "last_slot": {"$lt": slot},
                "$or": [{"owner": None}, {"lease_until": {"$lt": now}}],
            },
            {"$set": {"owner": self._owner, "lease_until": now + LEASE, "claimed_slot": slot}},
            return_document=ReturnDocument.AFTER,
        )
        return claimed is not None

    async def _execute(self, task: PeriodicTask, slot: datetime) -> None:
        failed = False
        for attempt in range(1, task.attempts + 1):
            try:
                if task.per_organization:
                    failed = not await self._for_each_organization(task)
                else:
                    await task.run()
                    failed = False
            except Exception as exc:
                failed = True
                logger.error(
                    "jobs.task_failed",
                    extra={"task": task.name, "attempt": attempt, "error_type": type(exc).__name__},
                )
            if not failed:
                break
        update: dict[str, object] = {
            "owner": None,
            "lease_until": utcnow(),
            "last_slot": slot,
            "last_run_at": utcnow(),
        }
        if failed:
            await self._leases.update_one(
                {ID: task.name, "owner": self._owner},
                {"$set": update | {"last_error_at": utcnow()}, "$inc": {"failures": 1}},
            )
        else:
            await self._leases.update_one({ID: task.name, "owner": self._owner}, {"$set": update})

    async def _for_each_organization(self, task: PeriodicTask) -> bool:
        organizations = self._database.collection(Organization.__collection__)
        ok = True
        async for item in organizations.find({"status": OrganizationStatus.ACTIVE.value}, projection={ID: 1}):
            with tenant_scope(item[ID]):
                try:
                    await task.run()
                except Exception as exc:
                    ok = False
                    logger.error(
                        "jobs.task_failed_for_organization",
                        extra={
                            "task": task.name,
                            "organization_id": str(item[ID]),
                            "error_type": type(exc).__name__,
                        },
                    )
        return ok
