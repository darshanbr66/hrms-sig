"""The worker's scheduler: registered tasks and schedules, run as the worker's database user,
and the same jobs running inside the API process (RUN_JOBS_IN_API, ADR-030)."""

import asyncio

from app.main import create_app
from app.modules.access.grant_requests import TASK_NAME as EXPIRE_GRANTS
from app.modules.identity.models import User, UserStatus
from app.modules.notify.dispatcher import SCHEDULE as DISPATCH_SCHEDULE
from app.modules.notify.dispatcher import TASK_NAME as DISPATCH_EMAIL
from app.modules.notify.public import EmailTemplate, enqueue
from app.platform.audit import integrity, witness
from app.platform.audit.records import AuditActor, AuditEvent
from app.platform.audit.writer import AuditWriter
from app.platform.clock import SystemClock
from app.platform.db import Database
from app.platform.email import OutgoingEmail
from app.platform.jobs import LEASES_COLLECTION, Scheduler
from app.worker import JobRunner, build_scheduler, email_renderers
from tests.api_support import RecordingSender, api_settings, create_organization, worker_settings
from tests.conftest import MongoServer, database_as, fresh_migrated_database


def scheduler_for(mongo: MongoServer, database: Database, name: str) -> Scheduler:
    settings = worker_settings(mongo.uri("hrms_worker"), name)
    return build_scheduler(settings, database, retention_database=None, sender=RecordingSender())


def schedules(scheduler: Scheduler) -> dict[str, str]:
    return {name: task.cron for name, task in scheduler._tasks.items()}


async def test_every_task_is_registered_with_its_schedule(migrated_mongo: MongoServer) -> None:
    name = await fresh_migrated_database(migrated_mongo)
    async with database_as(migrated_mongo, "hrms_worker", name) as database:
        registered = schedules(scheduler_for(migrated_mongo, database, name))
    assert registered[DISPATCH_EMAIL] == DISPATCH_SCHEDULE == "* * * * *"
    assert registered[EXPIRE_GRANTS] == "* * * * *"
    assert {task: registered[task] for task in integrity.SCHEDULES} == integrity.SCHEDULES
    assert registered[witness.TASK_NAME] == witness.SCHEDULE


def test_every_email_template_has_a_renderer() -> None:
    assert set(email_renderers("https://hrms.test", SystemClock())) == set(EmailTemplate)


async def test_the_sealer_runs_as_a_worker_task(migrated_mongo: MongoServer) -> None:
    name = await fresh_migrated_database(migrated_mongo)
    organization = await create_organization(migrated_mongo.uri("hrms_app"), name)
    async with (
        database_as(migrated_mongo, "hrms_app", name) as app,
        app.unit_of_work(tenant=organization) as session,
    ):
        await AuditWriter(SystemClock()).record(
            session, AuditEvent(action="test.worker.sealed", actor=AuditActor.system())
        )
    async with database_as(migrated_mongo, "hrms_worker", name) as database:
        ran = await scheduler_for(migrated_mongo, database, name).run_due()
        assert "audit.seal" in ran
        links = await database.collection("audit_chain_links").find({}).to_list()
        lease = await database.collection(LEASES_COLLECTION).find_one({"_id": "audit.seal"})
    assert [(link["stream"], link["position"]) for link in links] == [("audit_log", 1)]
    assert lease is not None
    assert lease["owner"] is None
    assert lease["failures"] == 0


async def test_a_slot_runs_once_across_schedulers(migrated_mongo: MongoServer) -> None:
    """A separate worker and the jobs inside the API can run at the same time: every slot of
    every task runs on one of them only (job leases)."""
    name = await fresh_migrated_database(migrated_mongo)
    async with database_as(migrated_mongo, "hrms_worker", name) as database:
        first, second = (
            scheduler_for(migrated_mongo, database, name),
            scheduler_for(migrated_mongo, database, name),
        )
        ran = await asyncio.gather(first.run_due(), second.run_due())
    assert sorted(ran[0] + ran[1]) == sorted(set(ran[0]) | set(ran[1]))
    assert set(ran[0]) | set(ran[1]) == set(schedules(first))


async def test_the_api_runs_the_jobs_when_asked(migrated_mongo: MongoServer) -> None:
    """RUN_JOBS_IN_API: the API's lifespan starts the scheduler (as the worker's database user) and
    stops it on shutdown; a queued email is sent without a separate worker."""
    name = await fresh_migrated_database(migrated_mongo)
    organization = await create_organization(migrated_mongo.uri("hrms_app"), name)
    delivered = asyncio.Event()

    class SignallingSender(RecordingSender):
        async def send(self, email: OutgoingEmail) -> None:
            await super().send(email)
            delivered.set()

    sender = SignallingSender()
    jobs = JobRunner(
        worker_settings(migrated_mongo.uri("hrms_worker"), name),
        database_uri=migrated_mongo.uri("hrms_worker"),
        application_name="test-api-jobs",
        sender=sender,
    )
    app = create_app(api_settings(migrated_mongo.uri("hrms_app"), name), jobs=jobs)
    async with app.router.lifespan_context(app):
        database: Database = app.state.database
        async with database.unit_of_work(tenant=organization) as session:
            now = SystemClock().now()
            user = User(email="queued@dev.example", status=UserStatus.ACTIVE.value, email_verified_at=now)
            session.add(user)
            await session.flush()
            await enqueue(
                session,
                user_id=user.id,
                template=EmailTemplate.PASSWORD_CHANGED,
                idempotency_key=f"test:{user.id}",
                now=now,
            )
        # The dispatcher runs as soon as the scheduler starts, then every minute.
        await asyncio.wait_for(delivered.wait(), timeout=30)
    assert [message.to for message in sender.sent] == ["queued@dev.example"]
    async with database_as(migrated_mongo, "hrms_worker", name) as worker:
        lease = await worker.collection(LEASES_COLLECTION).find_one({"_id": DISPATCH_EMAIL})
    assert lease is not None
    assert lease["owner"] is None  # released on shutdown, not held by a stopped process
