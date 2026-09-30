"""Worker process entry point: `python -m app.worker`."""

import asyncio
import sys

from app.platform.config import WorkerSettings
from app.platform.jobs import create_job_app
from app.platform.logging import configure_logging


async def run(settings: WorkerSettings) -> None:
    job_app = create_job_app(settings.database_url_worker, application_name="hrms-worker")
    async with job_app.open_async():
        await job_app.run_worker_async()


def main() -> None:
    settings = WorkerSettings()  # values come from the environment
    configure_logging(settings.log_level)
    # psycopg's async driver needs a selector event loop, which is not the Windows default.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(settings), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
