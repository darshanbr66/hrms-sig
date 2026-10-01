"""Worker process entry point: `python -m app.worker`."""

import asyncio
import sys

import procrastinate

from app.modules.identity.emails import IdentityEmails
from app.modules.notify.dispatcher import OutboxDispatcher, register_email_dispatch
from app.platform.audit.partitions import register_partition_maintenance
from app.platform.clock import SystemClock
from app.platform.config import WorkerSettings
from app.platform.db import Database, create_engine
from app.platform.email import EmailSender, SmtpSender
from app.platform.jobs import create_job_app
from app.platform.logging import configure_logging

APPLICATION_NAME = "hrms-worker"


def smtp_sender(settings: WorkerSettings) -> SmtpSender:
    return SmtpSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.smtp_from,
        security=settings.smtp_security,
        username=settings.smtp_username,
        password=settings.smtp_password,
    )


def build_job_app(
    settings: WorkerSettings, database: Database, *, sender: EmailSender | None = None
) -> procrastinate.App:
    """The worker's job app with every task and periodic schedule registered."""
    job_app = create_job_app(settings.database_url_worker, application_name=APPLICATION_NAME)
    register_partition_maintenance(job_app, database)
    clock = SystemClock()
    dispatcher = OutboxDispatcher(
        database=database,
        sender=sender or smtp_sender(settings),
        renderers=IdentityEmails(app_base_url=settings.app_base_url, clock=clock).renderers(),
        clock=clock,
    )
    register_email_dispatch(job_app, dispatcher)
    return job_app


async def run(settings: WorkerSettings) -> None:
    database = Database(create_engine(settings.database_url_worker, application_name=APPLICATION_NAME))
    try:
        job_app = build_job_app(settings, database)
        async with job_app.open_async():
            await job_app.run_worker_async()
    finally:
        await database.dispose()


def main() -> None:
    settings = WorkerSettings()  # values come from the environment
    configure_logging(settings.log_level)
    # psycopg's async driver needs a selector event loop, which is not the Windows default.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(settings), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
