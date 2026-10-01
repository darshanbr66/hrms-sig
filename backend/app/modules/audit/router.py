"""Read APIs for the audit log and security events (docs/api-architecture.md §8).

Both permissions are flagged R, so every read is itself recorded in the audit log, in the
same transaction as the read. Neither stream holds sensitive values (docs/security-
architecture.md §8 guarantee 5), so the rows are returned as stored, except the keyed hash
of an attempted email, which is never returned.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import ColumnElement, Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit.records import AuditActor, AuditEvent, RequestContext
from app.platform.audit.tables import audit_log, security_events
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.authz.route import requires
from app.platform.db import Database
from app.platform.pagination import decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1", tags=["audit"])

MAX_LIMIT: Final = 100


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditRecord(_Model):
    id: uuid.UUID
    recorded_at: datetime
    occurred_at: datetime
    actor_type: str
    actor_user_id: uuid.UUID | None
    session_id: uuid.UUID | None
    grant_request_id: uuid.UUID | None
    request_id: uuid.UUID | None
    ip: str | None
    user_agent: str | None
    action: str
    permission_used: str | None
    target_type: str | None
    target_id: str | None
    subject_employee_id: uuid.UUID | None
    outcome: str
    changes: dict[str, Any] | None
    reason: str | None


class AuditPage(_Model):
    items: list[AuditRecord]
    next_cursor: str | None


class SecurityRecord(_Model):
    id: uuid.UUID
    recorded_at: datetime
    occurred_at: datetime
    event_type: str
    severity: str
    user_id: uuid.UUID | None
    session_id: uuid.UUID | None
    request_id: uuid.UUID | None
    ip: str | None
    user_agent: str | None
    details: dict[str, Any] | None


class SecurityPage(_Model):
    items: list[SecurityRecord]
    next_cursor: str | None


def _next_cursor(rows: list[Any], limit: int) -> str | None:
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return encode_cursor((last["recorded_at"], last["id"]))


async def _page(
    session: AsyncSession,
    *,
    query: Select[Any],
    table: Any,
    conditions: list[ColumnElement[bool]],
    cursor: str | None,
    limit: int,
) -> list[Any]:
    position = decode_cursor(cursor)
    if position is not None:
        conditions.append(tuple_(table.c.recorded_at, table.c.id) < tuple_(*position))
    query = query.where(*conditions).order_by(table.c.recorded_at.desc(), table.c.id.desc()).limit(limit + 1)
    return list((await session.execute(query)).mappings().all())


def _range(table: Any, since: datetime | None, until: datetime | None) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if since is not None:
        conditions.append(table.c.recorded_at >= since)
    if until is not None:
        conditions.append(table.c.recorded_at < until)
    return conditions


async def _record_read(
    request: Request, session: AsyncSession, actor: Actor, permission: str, action: str
) -> None:
    writer: AuditWriter = request.app.state.audit_writer
    await writer.record(
        session,
        AuditEvent(
            action=action,
            actor=AuditActor.user(actor.user_id, session_id=actor.session_id),
            context=RequestContext.from_request(request),
            permission_used=permission,
        ),
    )


@router.get("/audit-log", response_model=AuditPage)
async def read_audit_log(
    *,
    request: Request,
    actor: Annotated[Actor, requires("audit.read")],
    actor_id: uuid.UUID | None = None,
    subject_employee_id: uuid.UUID | None = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    since: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> AuditPage:
    database: Database = request.app.state.database
    engine: Authorizer = request.app.state.authorizer
    conditions = _range(audit_log, since, until)
    if actor_id is not None:
        conditions.append(audit_log.c.actor_user_id == actor_id)
    if subject_employee_id is not None:
        conditions.append(audit_log.c.subject_employee_id == subject_employee_id)
    if action is not None:
        conditions.append(audit_log.c.action == action)
    async with database.unit_of_work() as session:
        granted = await engine.require(session, actor, "audit.read")
        rows = await _page(
            session,
            query=select(audit_log),
            table=audit_log,
            conditions=conditions,
            cursor=cursor,
            limit=limit,
        )
        await _record_read(request, session, actor, granted.key, "audit.log.read")
    items = [
        AuditRecord(**{**row, "ip": str(row["ip"]) if row["ip"] is not None else None})
        for row in rows[:limit]
    ]
    return AuditPage(items=items, next_cursor=_next_cursor(rows, limit))


@router.get("/security-events", response_model=SecurityPage)
async def read_security_events(
    *,
    request: Request,
    actor: Annotated[Actor, requires("security.event.read")],
    user_id: uuid.UUID | None = None,
    event_type: Annotated[str | None, Query(max_length=100)] = None,
    since: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> SecurityPage:
    database: Database = request.app.state.database
    engine: Authorizer = request.app.state.authorizer
    conditions = _range(security_events, since, until)
    if user_id is not None:
        conditions.append(security_events.c.user_id == user_id)
    if event_type is not None:
        conditions.append(security_events.c.event_type == event_type)
    columns = [column for column in security_events.c if column.name != "email_attempted_hash"]
    async with database.unit_of_work() as session:
        granted = await engine.require(session, actor, "security.event.read")
        rows = await _page(
            session,
            query=select(*columns),
            table=security_events,
            conditions=conditions,
            cursor=cursor,
            limit=limit,
        )
        await _record_read(request, session, actor, granted.key, "security.events.read")
    items = [
        SecurityRecord(**{**row, "ip": str(row["ip"]) if row["ip"] is not None else None})
        for row in rows[:limit]
    ]
    return SecurityPage(items=items, next_cursor=_next_cursor(rows, limit))
