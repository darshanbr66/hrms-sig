"""Worker process entry point: `python -m app.worker`."""

import asyncio
import logging
import sys

import procrastinate

# Registers every table, so foreign keys into other modules (such as org.locations) resolve.
import app.metadata  # noqa: F401
from app.modules.access.public import AccessEmails, register_grant_expiry
from app.modules.attendance.public import AttendanceEmails
from app.modules.audit.public import AuditEmails, alert_chain_mismatch
from app.modules.documents.public import register_document_scan
from app.modules.identity.emails import IdentityEmails
from app.modules.leave.public import LeaveEmails, register_leave_jobs
from app.modules.notify.dispatcher import OutboxDispatcher, register_email_dispatch
from app.modules.notify.public import EmailTemplate, Renderer
from app.modules.reports.public import register_exports
from app.platform.audit.canonical import Stream
from app.platform.audit.integrity import AuditIntegrity, register_audit_integrity
from app.platform.audit.partitions import register_partition_maintenance
from app.platform.audit.writer import AuditWriter
from app.platform.clock import Clock, SystemClock
from app.platform.config import WorkerSettings
from app.platform.db import Database, create_engine
from app.platform.email import EmailSender, SmtpSender
from app.platform.jobs import create_job_app
from app.platform.logging import configure_logging
from app.platform.scanning import ClamdScanner

logger = logging.getLogger(__name__)

APPLICATION_NAME = "hrms-worker"
# Jobs run side by side, so a long audit verification or a slow anchor upload never holds up
# emails or grant expiry. Each task still runs one at a time (its job lock).
CONCURRENCY = 4


def smtp_sender(settings: WorkerSettings) -> SmtpSender:
    return SmtpSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.smtp_from,
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
    settings: WorkerSettings, database: Database, retention_database: Database, clock: Clock
) -> AuditIntegrity:
    return AuditIntegrity(
        database=database,
        retention_database=retention_database,
        anchors=settings.anchor_store(),
        signer=settings.checkpoint_signer(),
        verifier=settings.signature_verifier(),
        clock=clock,
        audit=AuditWriter(clock),
        retention_months={
            Stream.AUDIT_LOG: settings.audit_log_retention_months,
            Stream.SECURITY_EVENTS: settings.security_events_retention_months,
        },
        on_mismatch=alert_chain_mismatch,
    )


def build_job_app(
    settings: WorkerSettings,
    database: Database,
    *,
    retention_database: Database,
    sender: EmailSender | None = None,
) -> procrastinate.App:
    """The worker's job app with every task and periodic schedule registered."""
    job_app = create_job_app(settings.database_url_worker, application_name=APPLICATION_NAME)
    register_partition_maintenance(job_app, database)
    clock = SystemClock()
    register_grant_expiry(job_app, database, clock, AuditWriter(clock))
    register_leave_jobs(job_app, database, clock, AuditWriter(clock))
    register_audit_integrity(job_app, audit_integrity(settings, database, retention_database, clock))
    dispatcher = OutboxDispatcher(
        database=database,
        sender=sender or smtp_sender(settings),
        renderers=email_renderers(settings.app_base_url, clock),
        clock=clock,
    )
    register_email_dispatch(job_app, dispatcher)
    store = settings.file_store()
    if store is not None:
        register_exports(job_app, database, store, clock)
    else:
        logger.warning("reports.exports_disabled", extra={"reason": "FILES_BUCKET is not set"})
    if store is not None and settings.clamav_host is not None:
        register_document_scan(
            job_app,
            database,
            store=store,
            scanner=ClamdScanner(settings.clamav_host, settings.clamav_port),
            clock=clock,
            audit=AuditWriter(clock),
        )
    else:
        # Uploaded documents stay waiting for a scan and cannot be downloaded (fail closed).
        logger.warning("documents.scan_disabled", extra={"reason": "FILES_BUCKET or CLAMAV_HOST is not set"})
    return job_app


async def run(settings: WorkerSettings) -> None:
    database = Database(create_engine(settings.database_url_worker, application_name=APPLICATION_NAME))
    # Audit retention runs as its own database role, on its own connections.
    retention_database = Database(
        create_engine(settings.database_url_audit_retention, application_name=f"{APPLICATION_NAME}-retention")
    )
    try:
        job_app = build_job_app(settings, database, retention_database=retention_database)
        async with job_app.open_async():
            await job_app.run_worker_async(concurrency=CONCURRENCY)
    finally:
        await database.dispose()
        await retention_database.dispose()


def main() -> None:
    settings = WorkerSettings()  # values come from the environment
    configure_logging(settings.log_level)
    # psycopg's async driver needs a selector event loop, which is not the Windows default.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(settings), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
