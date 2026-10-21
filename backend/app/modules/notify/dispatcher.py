"""Sending the email outbox (worker only).

A periodic job (every minute) calls `OutboxDispatcher.run_once`:

1. **Claim.** Up to BATCH due messages (pending and due, or sending with an expired lease),
   each with one atomic update that marks it `sending` with a lease and counts the attempt,
   or marks it `failed` when its attempts are used up. A message one worker claimed is no
   longer due for another, so several workers never claim the same one (a claim lost to another
   worker at the same moment is skipped). Messages are taken
   earliest `next_attempt_at` first and then oldest (UUIDv7 `id`). The dispatcher runs once
   for every organization, each in its own scope.
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

from app.modules.notify.models import EmailOutbox, EmailTemplate, OutboxStatus
from app.modules.notify.public import OutboxItem, Renderer
from app.platform import settings
from app.platform.clock import Clock
from app.platform.db import ConcurrentChangeError, Database, Session
from app.platform.email import EmailDeliveryError, EmailSender
from app.platform.jobs import Scheduler

logger = logging.getLogger(__name__)

BATCH: Final = 20
LEASE: Final = timedelta(minutes=5)
MAX_BACKOFF: Final = timedelta(hours=1)
TASK_NAME: Final = "notify.dispatch_email"
SCHEDULE: Final = "* * * * *"


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
        exhausted = {"$gte": ["$attempts", max_attempts]}
        due = {
            "$or": [
                {"status": OutboxStatus.PENDING.value, "next_attempt_at": {"$lte": now}},
                {"status": OutboxStatus.SENDING.value, "lease_expires_at": {"$lt": now}},
            ]
        }
        claim = [
            {
                "$set": {
                    "updated_at": now,
                    # Its last allowed attempt was abandoned (the worker stopped mid-send).
                    "status": {"$cond": [exhausted, OutboxStatus.FAILED.value, OutboxStatus.SENDING.value]},
                    "last_error": {"$cond": [exhausted, "LeaseExpired", "$last_error"]},
                    "lease_expires_at": {"$cond": [exhausted, None, now + LEASE]},
                    "attempts": {"$cond": [exhausted, "$attempts", {"$add": ["$attempts", 1]}]},
                }
            }
        ]
        claimed: list[object] = []
        for _ in range(BATCH):
            try:
                async with self._db.unit_of_work() as session:
                    row = await session.find_one_and_update(
                        EmailOutbox, due, claim, sort=[("next_attempt_at", 1), ("id", 1)]
                    )
            except ConcurrentChangeError:
                # Another dispatcher claimed this message at the same moment: it is theirs.
                continue
            if row is None:
                break
            if row.status == OutboxStatus.FAILED:
                logger.error(
                    "email.delivery_failed",
                    extra={"outbox_id": str(row.id), "template": row.template, "attempts": row.attempts},
                )
                continue
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

    async def _locked_row(self, session: Session, row_id: object) -> EmailOutbox | None:
        """The message, only if it is still claimed (a lease can expire under a very slow send,
        and another worker may have taken it)."""
        return await session.find_one(
            EmailOutbox, {"id": row_id, "status": OutboxStatus.SENDING.value}, for_update=True
        )

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


def register_email_dispatch(scheduler: Scheduler, dispatcher: OutboxDispatcher) -> None:
    """Register the every-minute dispatch, run for each organization."""

    @scheduler.periodic(TASK_NAME, SCHEDULE, per_organization=True)
    async def dispatch_email() -> None:
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
