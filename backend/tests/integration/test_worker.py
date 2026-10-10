"""The worker's job app: registered tasks and schedules, run as the worker role."""

import logging

import pytest
from pydantic import SecretStr

from app.modules.notify.public import EmailTemplate
from app.platform.audit import integrity
from app.platform.audit.partitions import QUEUE, SCHEDULE, TASK_NAME
from app.platform.audit.records import AuditActor, AuditEvent
from app.platform.audit.writer import AuditWriter
from app.platform.clock import SystemClock
from app.worker import build_job_app, email_renderers
from tests.api_support import worker_settings
from tests.conftest import PostgresServer, S3Server, database_as, fresh_migrated_database


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
        job_app = build_job_app(settings, database, retention_database=database)
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
        job_app = build_job_app(settings, database, retention_database=database)
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
        job_app = build_job_app(settings, database, retention_database=database)
    [periodic] = [
        task
        for key, task in job_app.periodic_registry.periodic_tasks.items()
        if key[0] == "notify.dispatch_email"
    ]
    assert periodic.cron == "* * * * *"
    assert job_app.tasks["notify.dispatch_email"].queue == "email"


async def test_grant_expiry_runs_every_minute(migrated_postgres: PostgresServer) -> None:
    settings = worker_settings(migrated_postgres.url("hrms_worker"))
    async with database_as(migrated_postgres, "hrms_worker") as database:
        job_app = build_job_app(settings, database, retention_database=database)
    [periodic] = [
        task
        for key, task in job_app.periodic_registry.periodic_tasks.items()
        if key[0] == "access.expire_grants"
    ]
    assert periodic.cron == "* * * * *"
    assert job_app.tasks["access.expire_grants"].queue == "access"


def test_every_email_template_has_a_renderer() -> None:
    assert set(email_renderers("https://hrms.test", SystemClock())) == set(EmailTemplate)


async def test_audit_integrity_jobs_are_scheduled(migrated_postgres: PostgresServer) -> None:
    settings = worker_settings(migrated_postgres.url("hrms_worker"))
    async with database_as(migrated_postgres, "hrms_worker") as database:
        job_app = build_job_app(settings, database, retention_database=database)
    schedules = {key[0]: task.cron for key, task in job_app.periodic_registry.periodic_tasks.items()}
    assert {name: schedules[name] for name in integrity.SCHEDULES} == integrity.SCHEDULES
    assert {job_app.tasks[name].queue for name in integrity.SCHEDULES} == {integrity.QUEUE}


async def test_the_sealer_runs_as_a_worker_task(postgres: PostgresServer, s3: S3Server) -> None:
    database_name = fresh_migrated_database(postgres)
    async with database_as(postgres, "hrms_app", database_name) as app, app.unit_of_work() as session:
        await AuditWriter(SystemClock()).record(
            session, AuditEvent(action="test.worker.sealed", actor=AuditActor.system())
        )
    settings = worker_settings(
        postgres.url("hrms_worker", database_name),
        database_url_audit_retention=postgres.url("hrms_audit_retention", database_name),
        s3_endpoint_url=s3.endpoint_url,
        audit_anchor_bucket=s3.create_anchor_bucket(),
        audit_anchor_access_key_id=SecretStr(s3.access_key_id),
        audit_anchor_secret_access_key=SecretStr(s3.secret_access_key),
    )
    async with (
        database_as(postgres, "hrms_worker", database_name) as database,
        database_as(postgres, "hrms_audit_retention", database_name) as retention,
    ):
        job_app = build_job_app(settings, database, retention_database=retention)
        async with job_app.open_async():
            await job_app.tasks["audit.seal"].defer_async(timestamp=0)
            await job_app.run_worker_async(
                queues=[integrity.QUEUE], wait=False, install_signal_handlers=False
            )
    with postgres.connect("postgres", database_name) as connection:
        links = connection.execute("SELECT stream, position FROM audit.chain_links").fetchall()
        jobs = connection.execute(
            "SELECT DISTINCT status::text FROM procrastinate.procrastinate_jobs "
            "WHERE task_name = 'audit.seal'"
        ).fetchall()
    assert links == [("audit_log", 1)]
    assert jobs == [("succeeded",)]
