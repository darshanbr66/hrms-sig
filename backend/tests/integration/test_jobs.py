"""The API role can enqueue Procrastinate jobs and the worker role can run them."""

import procrastinate

from app.platform.jobs import create_job_app
from tests.conftest import PostgresServer


async def test_app_role_defers_and_worker_role_runs(migrated_postgres: PostgresServer) -> None:
    api_jobs = create_job_app(migrated_postgres.url("hrms_app"), application_name="test-api")
    received: list[int] = []

    @api_jobs.task(name="tests.record_value", queue="tests")
    async def record_value(value: int) -> None:
        received.append(value)

    worker_jobs = api_jobs.with_connector(
        procrastinate.PsycopgConnector(conninfo=migrated_postgres.url("hrms_worker").get_secret_value())
    )

    async with api_jobs.open_async():
        await record_value.defer_async(value=7)

    async with worker_jobs.open_async():
        await worker_jobs.run_worker_async(queues=["tests"], wait=False, install_signal_handlers=False)

    assert received == [7]
