"""Sending the email outbox (worker only).

A periodic job (every minute) calls `OutboxDispatcher.run_once`:

1. **Claim.** In one short transaction, take up to BATCH due rows with
   `FOR UPDATE SKIP LOCKED` (pending and due, or sending with an expired lease), mark them
   `sending` with a lease and count the attempt. Several workers never claim the same row.
   Rows are taken in a fixed order, earliest `next_attempt_at` first and then oldest row
   (UUIDv7 `id`), so rows due at the same moment are claimed oldest first instead of in
   whatever order the database returns them.
2. **Render.** For each claimed row, in its own transaction: re-read and lock the row (it must
   still be ours), then call the template's renderer, which resolves the recipient address
   and may create a link token (only its hash is stored). This commits before the email
   leaves, so a delivered link always works.
3. **Send** outside any transaction, then record the result: `sent`, or back to `pending`
   with exponential backoff (1, 2, 4, 8 … minutes, at most an hour), or `failed` once the
   attempts reach `email.delivery.max_attempts`. A failed row stays in the table and is
   logged at error level; nothing disappears silently.

Delivery is at least once: if the worker stops after the server accepted an email but before
the row is marked sent, the lease expires and the email is sent again. Errors are recorded
as an exception class name only (server replies can contain addresses).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import procrastinate
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notify.models import EmailOutbox, EmailTemplate, OutboxStatus
from app.modules.notify.public import OutboxItem, Renderer
from app.platform import settings
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.email import EmailDeliveryError, EmailSender

logger = logging.getLogger(__name__)

BATCH: Final = 20
LEASE: Final = timedelta(minutes=5)
MAX_BACKOFF: Final = timedelta(hours=1)
TASK_NAME: Final = "notify.dispatch_email"
SCHEDULE: Final = "* * * * *"
QUEUE: Final = "email"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    sent: int = 0
    retried: int = 0
    failed: int = 0
    cancelled: int = 0


def backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** max(attempts - 1, 0)), MAX_BACKOFF)


class OutboxDispatcher:
    def __init__(
        self,
        *,
        database: Database,
        sender: EmailSender,
        renderers: Mapping[EmailTemplate, Renderer],
        clock: Clock,
    ) -> None:
        missing = set(EmailTemplate) - set(renderers)
        if missing:
            raise ValueError(f"no renderer for {sorted(missing)}")
        self._db = database
        self._sender = sender
        self._renderers = renderers
        self._clock = clock

    async def run_once(self) -> DispatchResult:
        claimed, max_attempts = await self._claim()
        counts = {"sent": 0, "retried": 0, "failed": 0, "cancelled": 0}
        for row_id in claimed:
            try:
                outcome = await self._deliver(row_id, max_attempts)
            except Exception as exc:
                # One bad row (a renderer bug, a database error) must not stop the batch or be
                # retried for ever: it counts as a failed attempt like a delivery error.
                logger.exception("email.render_error", extra={"outbox_id": str(row_id)})
                outcome = await self._record_failure(row_id, type(exc).__name__, max_attempts)
            counts[outcome] += 1
        return DispatchResult(**counts)

    async def _claim(self) -> tuple[list[object], int]:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            max_attempts = (await settings.load(session))[settings.EMAIL_MAX_ATTEMPTS]
            due = or_(
                (EmailOutbox.status == OutboxStatus.PENDING.value) & (EmailOutbox.next_attempt_at <= now),
                (EmailOutbox.status == OutboxStatus.SENDING.value) & (EmailOutbox.lease_expires_at < now),
            )
            query = (
                select(EmailOutbox)
                .where(due)
                .order_by(EmailOutbox.next_attempt_at, EmailOutbox.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            claimed: list[object] = []
            for row in (await session.execute(query)).scalars():
                row.updated_at = now
                if row.attempts >= max_attempts:
                    # Its last allowed attempt was abandoned (the worker stopped mid-send).
                    row.status = OutboxStatus.FAILED.value
                    row.lease_expires_at = None
                    row.last_error = "LeaseExpired"
                    logger.error(
                        "email.delivery_failed",
                        extra={"outbox_id": str(row.id), "template": row.template, "attempts": row.attempts},
                    )
                    continue
                row.status = OutboxStatus.SENDING.value
                row.attempts += 1
                row.lease_expires_at = now + LEASE
                claimed.append(row.id)
            return claimed, max_attempts

    async def _deliver(self, row_id: object, max_attempts: int) -> str:
        async with self._db.unit_of_work() as session:
            row = await self._locked_row(session, row_id)
            if row is None:
                return "cancelled"
            item = OutboxItem(
                id=row.id,
                user_id=row.user_id,
                template=EmailTemplate(row.template),
                data=row.template_data or {},
                created_at=row.created_at,
            )
            email = await self._renderers[item.template](session, item)
            if email is None:
                row.status = OutboxStatus.CANCELLED.value
                row.lease_expires_at = None
                row.updated_at = self._clock.now()
                return "cancelled"
        try:
            await self._sender.send(email)
        except EmailDeliveryError as exc:
            return await self._record_failure(row_id, exc.reason, max_attempts)
        await self._record(row_id, status=OutboxStatus.SENT, sent_at=self._clock.now())
        return "sent"

    async def _locked_row(self, session: AsyncSession, row_id: object) -> EmailOutbox | None:
        """The row, only if it is still claimed by this run (a lease can expire under a very
        slow send, and another worker may have taken it)."""
        query = (
            select(EmailOutbox)
            .where(EmailOutbox.id == row_id, EmailOutbox.status == OutboxStatus.SENDING.value)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await session.execute(query)).scalar_one_or_none()

    async def _record_failure(self, row_id: object, reason: str, max_attempts: int) -> str:
        async with self._db.unit_of_work() as session:
            row = await self._locked_row(session, row_id)
            if row is None:
                return "retried"
            now = self._clock.now()
            row.last_error = reason[:200]
            row.lease_expires_at = None
            row.updated_at = now
            if row.attempts >= max_attempts:
                row.status = OutboxStatus.FAILED.value
                logger.error(
                    "email.delivery_failed",
                    extra={
                        "outbox_id": str(row.id),
                        "template": row.template,
                        "attempts": row.attempts,
                        "error": reason,
                    },
                )
                return "failed"
            row.status = OutboxStatus.PENDING.value
            row.next_attempt_at = now + backoff(row.attempts)
            logger.warning(
                "email.delivery_retry",
                extra={
                    "outbox_id": str(row.id),
                    "template": row.template,
                    "attempts": row.attempts,
                    "error": reason,
                },
            )
            return "retried"

    async def _record(self, row_id: object, *, status: OutboxStatus, sent_at: datetime) -> None:
        async with self._db.unit_of_work() as session:
            row = await self._locked_row(session, row_id)
            if row is None:
                return
            row.status = status.value
            row.sent_at = sent_at
            row.lease_expires_at = None
            row.last_error = None
            row.updated_at = sent_at


def register_email_dispatch(job_app: procrastinate.App, dispatcher: OutboxDispatcher) -> None:
    """Register the every-minute dispatch on the worker's job app."""

    @job_app.periodic(cron=SCHEDULE, periodic_id=TASK_NAME)
    @job_app.task(name=TASK_NAME, queue=QUEUE, lock=TASK_NAME)
    async def dispatch_email(timestamp: int) -> None:
        result = await dispatcher.run_once()
        if result != DispatchResult():
            logger.info(
                "email.dispatched",
                extra={
                    "sent": result.sent,
                    "retried": result.retried,
                    "failed": result.failed,
                    "cancelled": result.cancelled,
                },
            )
