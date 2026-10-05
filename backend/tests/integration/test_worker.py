"""The worker's job app: registered tasks and schedules, run as the worker role."""

import logging

import pytest

from app.platform.audit.partitions import QUEUE, SCHEDULE, TASK_NAME
from app.worker import build_job_app
from tests.api_support import worker_settings
from tests.conftest import PostgresServer, database_as, fresh_migrated_database


def last_partition_key(postgres: PostgresServer, database: str) -> str:
    with postgres.connect("postgres", database) as connection:
        row = connection.execute(
            "SELECT to_char(date_trunc('month', now() AT TIME ZONE 'UTC') + interval '3 months', 'YYYY_MM')"
        ).fetchone()
    assert row is not None
    return str(row[0])


def partition_exists(postgres: PostgresServer, database: str, name: str) -> bool:
    with postgres.connect("postgres", database) as connection:
        row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (f"audit.{name}",)).fetchone()
    return bool(row and row[0])


async def test_partition_maintenance_is_scheduled_hourly(migrated_postgres: PostgresServer) -> None:
    settings = worker_settings(migrated_postgres.url("hrms_worker"))
    async with database_as(migrated_postgres, "hrms_worker") as database:
        job_app = build_job_app(settings, database)
    assert TASK_NAME in job_app.tasks
    [periodic] = [
        task for key, task in job_app.periodic_registry.periodic_tasks.items() if key[0] == TASK_NAME
    ]
    assert periodic.cron == SCHEDULE
    assert job_app.tasks[TASK_NAME].queue == QUEUE


async def test_partition_maintenance_task_recreates_missing_partitions(
    postgres: PostgresServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.platform.audit.partitions")
    database_name = fresh_migrated_database(postgres)
    key = last_partition_key(postgres, database_name)
    with postgres.connect("hrms_migrator", database_name) as connection:
        for stream in ("audit_log", "security_events"):
            connection.execute(f"DROP TABLE audit.{stream}_p{key}")

    settings = worker_settings(postgres.url("hrms_worker", database_name))
    async with database_as(postgres, "hrms_worker", database_name) as database:
        job_app = build_job_app(settings, database)
        async with job_app.open_async():
            await job_app.tasks[TASK_NAME].defer_async(timestamp=0)
            await job_app.run_worker_async(queues=[QUEUE], wait=False, install_signal_handlers=False)

    for stream in ("audit_log", "security_events"):
        assert partition_exists(postgres, database_name, f"{stream}_p{key}")
    # Starting the worker also defers the hourly run for the current slot. Depending on timing
    # it runs before the worker stops, or stays queued without being attempted: one or two
    # runs, all legitimate. The invariants: every attempted run succeeded (a failure after the
    # partitions commit, such as an `extra` key that clashes with a LogRecord attribute, leaves
    # the job queued for retry with an attempt counted), each run logged its outcome, and
    # together they created exactly the two missing partitions.
    with postgres.connect("postgres", database_name) as connection:
        jobs = connection.execute(
            "SELECT status::text, attempts FROM procrastinate.procrastinate_jobs WHERE task_name = %s",
            (TASK_NAME,),
        ).fetchall()
    attempted = [status for status, attempts in jobs if not (status == "todo" and attempts == 0)]
    assert attempted
    assert set(attempted) == {"succeeded"}
    created = [
        record.__dict__["partitions_created"]
        for record in caplog.records
        if record.msg == "audit.partitions_ensured"
    ]
    assert len(created) == len(attempted)
    assert sum(created) == 2


async def test_the_email_outbox_is_dispatched_every_minute(migrated_postgres: PostgresServer) -> None:
    settings = worker_settings(migrated_postgres.url("hrms_worker"))
    async with database_as(migrated_postgres, "hrms_worker") as database:
        job_app = build_job_app(settings, database)
    [periodic] = [
        task
        for key, task in job_app.periodic_registry.periodic_tasks.items()
        if key[0] == "notify.dispatch_email"
    ]
    assert periodic.cron == "* * * * *"
    assert job_app.tasks["notify.dispatch_email"].queue == "email"
