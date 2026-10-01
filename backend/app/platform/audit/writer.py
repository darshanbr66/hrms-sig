"""Audit writer (docs/security-architecture.md §8).

- `record` and `record_security_event` write inside the caller's unit of work, so the
  audit row commits with the change it describes, and a failed audit write rolls the change
  back (guarantee 1).
- `record_separately` writes in its own short transaction, for records that must survive
  the request failing: denials on sensitive resources, failed sign-ins (guarantee 2).

The database sets `id` and `recorded_at` (the partition key); the writer never sends them.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.audit.records import AuditEvent, SecurityEvent
from app.platform.audit.tables import audit_log, security_events
from app.platform.clock import Clock
from app.platform.db import Database


class AuditTransactionRequiredError(RuntimeError):
    """An audit record was written outside a unit of work, so it could not commit with the change."""


@dataclass(frozen=True, slots=True)
class RecordRef:
    """Primary key of a written record: `id` plus the partition key."""

    id: uuid.UUID
    recorded_at: datetime


class AuditWriter:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def record(self, session: AsyncSession, event: AuditEvent) -> RecordRef:
        """Write a business audit record in the caller's transaction."""
        self._require_transaction(session)
        statement = (
            insert(audit_log)
            .values(
                occurred_at=event.occurred_at or self._clock.now(),
                actor_type=event.actor.type.value,
                actor_user_id=event.actor.user_id,
                session_id=event.actor.session_id,
                grant_request_id=event.actor.grant_request_id,
                request_id=event.context.request_id,
                ip=str(event.context.ip) if event.context.ip else None,
                user_agent=event.context.user_agent,
                action=event.action,
                permission_used=event.permission_used,
                target_type=event.target_type,
                target_id=str(event.target_id) if event.target_id is not None else None,
                subject_employee_id=event.subject_employee_id,
                outcome=event.outcome.value,
                changes=event.changes_document(),
                reason=event.reason,
            )
            .returning(audit_log.c.id, audit_log.c.recorded_at)
        )
        row = (await session.execute(statement)).one()
        return RecordRef(row.id, row.recorded_at)

    async def record_security_event(self, session: AsyncSession, event: SecurityEvent) -> RecordRef:
        """Write a security event in the caller's transaction."""
        self._require_transaction(session)
        statement = (
            insert(security_events)
            .values(
                occurred_at=event.occurred_at or self._clock.now(),
                event_type=event.event_type.value,
                severity=event.severity.value,
                user_id=event.user_id,
                session_id=event.session_id,
                request_id=event.context.request_id,
                email_attempted_hash=event.email_attempted_hash,
                ip=str(event.context.ip) if event.context.ip else None,
                user_agent=event.context.user_agent,
                details=event.details_document(),
            )
            .returning(security_events.c.id, security_events.c.recorded_at)
        )
        row = (await session.execute(statement)).one()
        return RecordRef(row.id, row.recorded_at)

    async def record_separately(self, database: Database, event: AuditEvent | SecurityEvent) -> RecordRef:
        """Write in a separate, immediately committed transaction.

        For records that must outlive a failing request. It commits even if the caller's own
        unit of work later rolls back; a failure here propagates to the caller.
        """
        async with database.unit_of_work() as session:
            if isinstance(event, AuditEvent):
                return await self.record(session, event)
            return await self.record_security_event(session, event)

    @staticmethod
    def _require_transaction(session: AsyncSession) -> None:
        if not session.in_transaction():
            raise AuditTransactionRequiredError(
                "audit records are written inside a unit of work (Database.unit_of_work)"
            )
