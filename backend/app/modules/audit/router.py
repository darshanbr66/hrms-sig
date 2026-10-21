"""Read APIs for the audit log and security events (docs/api-architecture.md §8).

Both permissions are flagged R, so every read is itself recorded in the audit log, in the
same transaction as the read. Neither stream holds sensitive values (docs/security-
architecture.md §8 guarantee 5), so the records are returned as stored, except the keyed hash
of an attempted email, which is never returned. Each reader sees their own organization's
records only; security events that belong to no organization (a sign-in attempt for an
unknown email) are not listed here.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.platform.audit.records import AuditEvent, RequestContext
from app.platform.audit.tables import AuditLogRecord, SecurityEventRecord
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.authz.route import requires
from app.platform.db import Database, Session
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
    return encode_cursor((last.recorded_at, last.id))


async def _page(
    session: Session,
    model: type[AuditLogRecord] | type[SecurityEventRecord],
    conditions: dict[str, Any],
    *,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
    limit: int,
) -> list[Any]:
    parts: list[dict[str, Any]] = [conditions]
    if since is not None:
        parts.append({"recorded_at": {"$gte": since}})
    if until is not None:
        parts.append({"recorded_at": {"$lt": until}})
    position = decode_cursor(cursor)
    if position is not None:
        parts.append(
            {
                "$or": [
                    {"recorded_at": {"$lt": position[0]}},
                    {"recorded_at": position[0], "id": {"$lt": position[1]}},
                ]
            }
        )
    return await session.find(model, {"$and": parts}, sort=[("recorded_at", -1), ("id", -1)], limit=limit + 1)


async def _record_read(
    request: Request, session: Session, actor: Actor, permission: str, action: str
) -> None:
    writer: AuditWriter = request.app.state.audit_writer
    await writer.record(
        session,
        AuditEvent(
            action=action,
            actor=actor.audit_actor(),
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
    conditions: dict[str, Any] = {}
    if actor_id is not None:
        conditions["actor_user_id"] = actor_id
    if subject_employee_id is not None:
        conditions["subject_employee_id"] = subject_employee_id
    if action is not None:
        conditions["action"] = action
    async with database.unit_of_work() as session:
        granted = await engine.require(session, actor, "audit.read")
        rows = await _page(
            session, AuditLogRecord, conditions, since=since, until=until, cursor=cursor, limit=limit
        )
        await _record_read(request, session, actor, granted.key, "audit.log.read")
    items = [
        AuditRecord(**{name: getattr(row, name) for name in AuditRecord.model_fields}) for row in rows[:limit]
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
    async with database.unit_of_work() as session:
        # Security events are not tenant-scoped (some belong to no organization): name it here.
        conditions: dict[str, Any] = {"organization_id": session.require_tenant()}
        if user_id is not None:
            conditions["user_id"] = user_id
        if event_type is not None:
            conditions["event_type"] = event_type
        granted = await engine.require(session, actor, "security.event.read")
        rows = await _page(
            session, SecurityEventRecord, conditions, since=since, until=until, cursor=cursor, limit=limit
        )
        await _record_read(request, session, actor, granted.key, "security.events.read")
    # The keyed hash of an attempted email is never returned (SecurityRecord has no such field).
    items = [
        SecurityRecord(**{name: getattr(row, name) for name in SecurityRecord.model_fields})
        for row in rows[:limit]
    ]
    return SecurityPage(items=items, next_cursor=_next_cursor(rows, limit))
