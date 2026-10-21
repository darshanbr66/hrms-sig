"""The worker: every scheduled task (app/platform/jobs.py), in one of two hosts.

- `python -m app.worker`, a process of its own, runs until it receives SIGTERM or SIGINT, then
  finishes the task in progress and stops.
- `JobRunner`, inside the API process when `RUN_JOBS_IN_API=true` (ADR-030): the same scheduler
  as one background task of the API, started and stopped with the API's lifespan. Its heartbeat
  (`JobHealth`) is part of the API's readiness, and a runner that ends without being asked to
  stops the API process, so the platform restarts it rather than serving without jobs.

Either way, every slot of every task runs once across all hosts (job leases), so a separate
worker and in-API jobs can run at the same time without doing anything twice. Per-organization
tasks run once for each active organization, inside that organization's scope.
"""

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable
from typing import Final

import httpx
from pydantic import SecretStr

from app.modules.access.public import AccessEmails, register_grant_expiry
from app.modules.attendance.public import AttendanceEmails
from app.modules.audit.public import AuditEmails, chain_mismatch_alert
from app.modules.documents.public import register_document_scan
from app.modules.identity.emails import IdentityEmails
from app.modules.leave.public import LeaveEmails, register_leave_jobs
from app.modules.notify.dispatcher import OutboxDispatcher, register_email_dispatch
from app.modules.notify.public import EmailTemplate, Renderer
from app.modules.reports.public import register_exports
from app.platform.audit.canonical import Stream
from app.platform.audit.integrity import AuditIntegrity, register_audit_integrity
from app.platform.audit.witness import register_audit_witness
from app.platform.audit.writer import AuditWriter
from app.platform.clock import Clock, SystemClock
from app.platform.config import EmailTransport, WorkerSettings
from app.platform.db import Database, create_client
from app.platform.email import EmailSender, ResendSender, SmtpSender
from app.platform.files import GridFsFileStore
from app.platform.jobs import JobHealth, Scheduler
from app.platform.logging import configure_logging
from app.platform.scanning import ClamdScanner

logger = logging.getLogger(__name__)

APPLICATION_NAME = "hrms-worker"
# How long a stopping API waits for the task in progress before cancelling it. Render sends
# SIGKILL 30 seconds after SIGTERM; a cancelled task's lease expires and its slot runs again.
STOP_SECONDS: Final = 20
# Pause before the scheduler is restarted after an unexpected error.
RESTART_SECONDS: Final = 30


def email_sender(settings: WorkerSettings, client: httpx.AsyncClient) -> EmailSender:
    """The configured transport: Resend's HTTPS API, or SMTP (local and test only)."""
    if settings.email_transport is EmailTransport.RESEND:
        if settings.resend_api_key is None:  # WorkerSettings refuses this; kept for the type
            raise ValueError("RESEND_API_KEY is not set")
        return ResendSender(client=client, api_key=settings.resend_api_key, sender=settings.email_from)
    if settings.smtp_host is None:
        raise ValueError("SMTP_HOST is not set")
    return SmtpSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.email_from,
        security=settings.smtp_security,
        username=settings.smtp_username,
        password=settings.smtp_password,
    )


def email_renderers(app_base_url: str, clock: Clock) -> dict[EmailTemplate, Renderer]:
    """Every email template's renderer (the dispatcher refuses to start with one missing)."""
    return {
        **IdentityEmails(app_base_url=app_base_url, clock=clock).renderers(),
        **AccessEmails(app_base_url=app_base_url).renderers(),
        **AuditEmails().renderers(),
        **LeaveEmails(app_base_url=app_base_url).renderers(),
        **AttendanceEmails(app_base_url=app_base_url).renderers(),
    }


def audit_integrity(
    settings: WorkerSettings, database: Database, retention_database: Database | None, clock: Clock
) -> AuditIntegrity:
    return AuditIntegrity(
        database=database,
        retention_database=retention_database,
        signer=settings.checkpoint_signer(),
        verifier=settings.signature_verifier(),
        clock=clock,
        audit=AuditWriter(clock),
        retention_months={
            Stream.AUDIT_LOG: settings.audit_log_retention_months,
            Stream.SECURITY_EVENTS: settings.security_events_retention_months,
        },
        on_mismatch=chain_mismatch_alert(database),
    )


def build_scheduler(
    settings: WorkerSettings,
    database: Database,
    *,
    retention_database: Database | None,
    sender: EmailSender,
    clock: Clock | None = None,
    health: JobHealth | None = None,
) -> Scheduler:
    """The worker's scheduler with every task registered; it beats `health` while it runs."""
    clock = clock or SystemClock()
    scheduler = Scheduler(database, clock, health=health)
    register_grant_expiry(scheduler, database, clock, AuditWriter(clock))
    register_leave_jobs(scheduler, database, clock, AuditWriter(clock))
    register_audit_integrity(scheduler, audit_integrity(settings, database, retention_database, clock))
    register_audit_witness(
        scheduler,
        database,
        sender=sender,
        recipients=settings.audit_witness_emails,
        verifier=settings.signature_verifier(),
        clock=clock,
    )
    dispatcher = OutboxDispatcher(
        database=database,
        sender=sender,
        renderers=email_renderers(settings.app_base_url, clock),
        clock=clock,
    )
    register_email_dispatch(scheduler, dispatcher)
    store = GridFsFileStore(database)
    register_exports(scheduler, database, store, clock)
    if settings.document_uploads_enabled and settings.clamav_host is not None:
        register_document_scan(
            scheduler,
            database,
            store=store,
            scanner=ClamdScanner(settings.clamav_host, settings.clamav_port),
            clock=clock,
            audit=AuditWriter(clock),
        )
    else:
        # Uploads are refused while scanning is off; anything uploaded earlier stays waiting for
        # a scan and cannot be downloaded (fail closed).
        logger.warning(
            "documents.scan_disabled", extra={"reason": "uploads are off or CLAMAV_HOST is not set"}
        )
    return scheduler


def terminate_process() -> None:
    """Ask this process to shut down (SIGTERM, as the platform would), so it is restarted."""
    os.kill(os.getpid(), signal.SIGTERM)


class JobRunner:
    """The scheduler with its own database connections and HTTP client, run until stopped.

    `start` runs it as a background task of the current event loop (the API's lifespan); a
    second `start` is refused, so one process never runs two schedulers. Any unexpected error,
    while connecting, building the scheduler or running it, restarts all of it after a pause
    instead of silently ending the jobs. `health` says whether it is running: the scheduler
    beats it, and a task that ends without `stop` marks it failed, logs `jobs.runner_stopped`
    and calls `on_unexpected_exit` (the API passes `terminate_process`). `stop` lets the task in
    progress finish for up to `STOP_SECONDS`, then cancels it; its lease expires and its slot
    runs again, as for a worker that died (every task is idempotent)."""

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        database_uri: SecretStr,
        application_name: str,
        sender: EmailSender | None = None,
        on_unexpected_exit: Callable[[], None] | None = None,
    ) -> None:
        """`sender` replaces the configured email transport (tests)."""
        self._settings = settings
        self._database_uri = database_uri
        self._application_name = application_name
        self._sender = sender
        self._on_unexpected_exit = on_unexpected_exit
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.health = JobHealth()

    async def run(self) -> None:
        self.health.beat()
        while not self._stop.is_set():
            try:
                await self._run_once()
            except Exception as exc:
                logger.error("jobs.scheduler_failed", extra={"error_type": type(exc).__name__})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=RESTART_SECONDS)

    async def _run_once(self) -> None:
        """Connect, build the scheduler and run it until stopped; everything opened is closed."""
        settings = self._settings
        client = httpx.AsyncClient()
        database: Database | None = None
        retention_database: Database | None = None
        try:
            database = Database(
                create_client(self._database_uri, application_name=self._application_name),
                settings.mongodb_database,
            )
            # Audit retention runs as its own database user, on its own connections.
            if settings.mongodb_audit_retention_uri is not None:
                retention_database = Database(
                    create_client(
                        settings.mongodb_audit_retention_uri,
                        application_name=f"{self._application_name}-retention",
                    ),
                    settings.mongodb_database,
                )
            scheduler = build_scheduler(
                settings,
                database,
                retention_database=retention_database,
                sender=self._sender or email_sender(settings, client),
                health=self.health,
            )
            await scheduler.run_forever(self._stop)
        finally:
            await client.aclose()
            if database is not None:
                await database.dispose()
            if retention_database is not None:
                await retention_database.dispose()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("the jobs are already running in this process")
        self._task = asyncio.create_task(self.run(), name="hrms-jobs")
        self._task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if self._stop.is_set():
            return
        if task.cancelled():
            reason = "cancelled"
        else:
            error = task.exception()
            reason = type(error).__name__ if error is not None else "returned"
        self.health.fail(reason)
        logger.critical("jobs.runner_stopped", extra={"reason": reason})
        if self._on_unexpected_exit is not None:
            self._on_unexpected_exit()

    def request_stop(self) -> None:
        self._stop.set()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        task, self._task = self._task, None
        if task.done():
            return  # it ended on its own, and `_finished` has reported it
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=STOP_SECONDS)
        except TimeoutError:
            logger.warning("jobs.stop_timeout", extra={"seconds": STOP_SECONDS})
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def run(settings: WorkerSettings) -> None:
    runner = JobRunner(settings, database_uri=settings.mongodb_uri, application_name=APPLICATION_NAME)
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            with contextlib.suppress(NotImplementedError):  # Windows has no loop signal handlers
                loop.add_signal_handler(getattr(signal, name), runner.request_stop)
    await runner.run()


def main() -> None:
    settings = WorkerSettings()  # values come from the environment
    configure_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
