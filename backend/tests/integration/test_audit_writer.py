"""Audit writer against the real audit tables (docs/security-architecture.md §8).

Guarantee 1: an audit record commits with the change it describes, and a failed audit
write rolls the change back. Guarantee 2: a record written separately survives the
request's own transaction rolling back.
"""

import ipaddress
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.modules.org.models import Location
from app.platform.audit.records import (
    ActorType,
    AuditActor,
    AuditEvent,
    Outcome,
    RedactedChange,
    RequestContext,
    SecurityEvent,
    SecurityEventType,
    Severity,
    ValueChange,
)
from app.platform.audit.tables import audit_log, security_events
from app.platform.audit.writer import AuditTransactionRequiredError, AuditWriter
from app.platform.clock import SystemClock
from app.platform.db import Database
from tests.conftest import PostgresServer, database_as, fresh_migrated_database
from tests.people_data import fake_code

FIXED_TIME = datetime(2030, 3, 1, 9, 30, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_TIME


def web_context() -> RequestContext:
    return RequestContext(
        request_id=uuid.uuid4(), ip=ipaddress.ip_address("203.0.113.7"), user_agent="Test browser"
    )


async def audit_row(session: AsyncSession, record_id: uuid.UUID) -> dict[str, Any]:
    row = (await session.execute(select(audit_log).where(audit_log.c.id == record_id))).mappings().one()
    return dict(row)


async def count_audit_rows(database: Database, request_id: uuid.UUID) -> int:
    async with database.unit_of_work() as session:
        query = select(func.count()).select_from(audit_log).where(audit_log.c.request_id == request_id)
        return int((await session.execute(query)).scalar_one())


async def test_records_every_field(app_database: Database) -> None:
    writer = AuditWriter(FixedClock())
    actor = AuditActor.user(uuid.uuid4(), session_id=uuid.uuid4(), grant_request_id=uuid.uuid4())
    subject = uuid.uuid4()
    context = web_context()
    event = AuditEvent(
        action="employee.job.changed",
        actor=actor,
        context=context,
        target_type="employee_job",
        target_id=uuid.uuid4(),
        subject_employee_id=subject,
        permission_used="employee.job.manage.all",
        changes={
            "designation_id": ValueChange(uuid.UUID(int=1), uuid.UUID(int=2)),
            "effective_from": ValueChange(None, date(2030, 4, 1)),
            "grade_steps": ValueChange(Decimal("1.50"), Decimal("2.00")),
            "bank_account": RedactedChange(),
        },
        reason="Test reason",
    )
    async with app_database.unit_of_work() as session:
        ref = await writer.record(session, event)
        stored_now: datetime = (await session.execute(text("SELECT now()"))).scalar_one()

    async with app_database.unit_of_work() as session:
        row = await audit_row(session, ref.id)
    assert ref.id.version == 7
    assert row["recorded_at"] == ref.recorded_at == stored_now
    assert row["occurred_at"] == FIXED_TIME
    assert row["actor_type"] == "user"
    assert (row["actor_user_id"], row["session_id"], row["grant_request_id"]) == (
        actor.user_id,
        actor.session_id,
        actor.grant_request_id,
    )
    assert row["request_id"] == context.request_id
    assert row["ip"] == context.ip
    assert row["user_agent"] == "Test browser"
    assert row["target_id"] == str(event.target_id)
    assert row["subject_employee_id"] == subject
    assert row["outcome"] == "success"
    assert row["changes"] == {
        "designation_id": {"old": str(uuid.UUID(int=1)), "new": str(uuid.UUID(int=2))},
        "effective_from": {"old": None, "new": "2030-04-01"},
        "grade_steps": {"old": "1.50", "new": "2.00"},
        "bank_account": {"redacted": True},
    }
    assert row["reason"] == "Test reason"


async def test_records_a_security_event(app_database: Database) -> None:
    writer = AuditWriter(FixedClock())
    user_id = uuid.uuid4()
    event = SecurityEvent(
        event_type=SecurityEventType.LOGIN_FAILED,
        severity=Severity.WARNING,
        context=web_context(),
        user_id=user_id,
        email_attempted_hash=bytes(32),
        details={"failed_attempts": 3, "locked": False},
    )
    async with app_database.unit_of_work() as session:
        ref = await writer.record_security_event(session, event)
    async with app_database.unit_of_work() as session:
        query = select(security_events).where(security_events.c.id == ref.id)
        row = (await session.execute(query)).mappings().one()
    assert row["event_type"] == "login.failed"
    assert row["severity"] == "warning"
    assert row["user_id"] == user_id
    assert row["email_attempted_hash"] == bytes(32)
    assert row["details"] == {"failed_attempts": 3, "locked": False}
    assert row["occurred_at"] == FIXED_TIME


async def test_a_system_record_needs_no_request(app_database: Database) -> None:
    writer = AuditWriter(SystemClock())
    async with app_database.unit_of_work() as session:
        ref = await writer.record(
            session, AuditEvent(action="settings.value.changed", actor=AuditActor.system())
        )
        row = await audit_row(session, ref.id)
    assert row["actor_type"] == ActorType.SYSTEM.value
    assert row["request_id"] is None
    assert row["ip"] is None
    assert abs(row["occurred_at"] - datetime.now(UTC)) < timedelta(minutes=1)
    # Regression: an absent change list is SQL NULL, not a JSON null value.
    async with app_database.unit_of_work() as session:
        is_sql_null = select(audit_log.c.changes.is_(None)).where(audit_log.c.id == ref.id)
        assert (await session.execute(is_sql_null)).scalar_one() is True


async def test_a_failed_change_leaves_no_audit_record(app_database: Database) -> None:
    writer = AuditWriter(SystemClock())
    context = web_context()
    duplicate = fake_code()
    async with app_database.unit_of_work() as session:
        session.add(Location(code=duplicate, name="Test", time_zone="UTC", country_code="IN"))

    async def audit_then_fail() -> None:
        async with app_database.unit_of_work() as session:
            await writer.record(
                session, AuditEvent(action="org.location.created", actor=AuditActor.system(), context=context)
            )
            session.add(Location(code=duplicate, name="Test", time_zone="UTC", country_code="IN"))
            await session.flush()

    with pytest.raises(DBAPIError):
        await audit_then_fail()

    assert await count_audit_rows(app_database, context.request_id or uuid.uuid4()) == 0


async def test_a_failed_audit_write_rolls_the_change_back(postgres: PostgresServer) -> None:
    """With the current month's partition missing, the audit insert fails, and the business
    row written earlier in the same unit of work is rolled back with it."""
    name = fresh_migrated_database(postgres)
    with postgres.connect("hrms_migrator", name) as connection:
        key = connection.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY_MM')").fetchone()
        assert key is not None
        connection.execute(f"ALTER TABLE audit.audit_log DETACH PARTITION audit.audit_log_p{key[0]}")

    code = fake_code()
    writer = AuditWriter(SystemClock())
    async with database_as(postgres, "hrms_app", name) as database:

        async def change_then_audit() -> None:
            async with database.unit_of_work() as session:
                session.add(Location(code=code, name="Test", time_zone="UTC", country_code="IN"))
                await session.flush()
                await writer.record(
                    session, AuditEvent(action="org.location.created", actor=AuditActor.system())
                )

        with pytest.raises(DBAPIError, match="no partition"):
            await change_then_audit()
        async with database.unit_of_work() as session:
            query = select(func.count()).select_from(Location).where(Location.code == code)
            assert (await session.execute(query)).scalar_one() == 0


async def test_a_separate_record_survives_the_request_rolling_back(app_database: Database) -> None:
    writer = AuditWriter(SystemClock())
    context = web_context()
    denial = AuditEvent(
        action="employee.personal.read",
        actor=AuditActor.user(uuid.uuid4()),
        context=context,
        outcome=Outcome.DENIED,
        permission_used="employee.personal.read.all",
        subject_employee_id=uuid.uuid4(),
    )
    failed_login = SecurityEvent(
        event_type=SecurityEventType.LOGIN_FAILED, severity=Severity.INFO, context=context
    )

    class RequestFailedError(Exception):
        pass

    async def failing_request() -> None:
        async with app_database.unit_of_work():
            await writer.record_separately(app_database, denial)
            await writer.record_separately(app_database, failed_login)
            raise RequestFailedError

    with pytest.raises(RequestFailedError):
        await failing_request()

    assert await count_audit_rows(app_database, context.request_id or uuid.uuid4()) == 1
    async with app_database.unit_of_work() as session:
        query = (
            select(func.count())
            .select_from(security_events)
            .where(security_events.c.request_id == context.request_id)
        )
        assert (await session.execute(query)).scalar_one() == 1


async def test_writing_outside_a_unit_of_work_is_refused(app_database: Database) -> None:
    writer = AuditWriter(SystemClock())
    async with AsyncSession(app_database.engine) as session:
        with pytest.raises(AuditTransactionRequiredError):
            await writer.record(session, AuditEvent(action="test.probe", actor=AuditActor.system()))
        with pytest.raises(AuditTransactionRequiredError):
            await writer.record_security_event(
                session, SecurityEvent(event_type=SecurityEventType.LOGIN_FAILED, severity=Severity.INFO)
            )


def test_request_context_comes_from_the_request_middleware_state() -> None:
    request_id = uuid.uuid4()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"user-agent", b"x" * 600)],
            "state": {"request_id": request_id.hex, "client_ip": ipaddress.ip_address("198.51.100.4")},
        }
    )
    context = RequestContext.from_request(request)
    assert context.request_id == request_id
    assert context.ip == ipaddress.ip_address("198.51.100.4")
    assert context.user_agent == "x" * 512

    bare = RequestContext.from_request(Request({"type": "http", "method": "GET", "path": "/", "headers": []}))
    assert bare == RequestContext()
