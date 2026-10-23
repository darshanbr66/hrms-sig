"""Permanently erasing one person's data (owner decision 2026-10-23; docs/database-design.md
§9.1, docs/authorization-model.md §4.2, docs/security-architecture.md §3.9).

Account disabling and the one-step exit are separate operations and stay as they are; erasure
comes after them and is the only path that removes a person's data.

- Only super admins, and two of them: one requests (`employee.erasure.request`), a different
  one approves and runs it (`employee.erasure.approve`). Both need step-up, both type the
  person's employee code, and the account acting must hold `super_admin` whatever else grants
  the permission. Neither may be the person. The permissions read no data: a request shows the
  person's name and code only, so both can confirm who it is.
- Preconditions, checked when requested and again under the employee record's lock when
  approved: the person has left (exited, last working day passed), their data was not erased
  already, and their account (if linked) holds neither `super_admin` nor `system_admin`: those
  are removed first, by another super admin, under the two-active-super-admin rule. One open
  request per person (unique index).
- Approval runs every module's part in one transaction (`erase`): records are deleted or
  anonymized, history that must not change is kept under the bare ID, and the stored files are
  marked erased. If any step fails nothing is changed and the request is still waiting, so it
  can be approved again. The worker then removes the files (`job.py`); the request is complete
  only once none is left, and its result lists every category with what happened and why.
- The audit records name the request, the employee ID and counts, never the person's details.
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.modules.access import public as access
from app.modules.attendance import public as attendance
from app.modules.documents import public as documents
from app.modules.erasure.models import ErasureRequest, ErasureStatus
from app.modules.identity import public as identity
from app.modules.leave import public as leave
from app.modules.notify import public as notify
from app.modules.org import public as org
from app.modules.people import public as people
from app.modules.reports import public as reports
from app.platform.audit.records import AuditEvent, RequestContext, ValueChange
from app.platform.audit.tables import AuditLogRecord, SecurityEventRecord
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer, Granted
from app.platform.authz.roles import ADMIN_ACCOUNT_ROLES, RoleKey
from app.platform.authz.sod import SodRule
from app.platform.clock import Clock
from app.platform.db import Database, Session, UniqueViolationError
from app.platform.erasure import Category, Erased, Outcome
from app.platform.errors import ProblemError, ProblemType


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


@dataclass(frozen=True, slots=True)
class ErasureRecord:
    request: ErasureRequest
    # The person as the record names them now: their name and code until the erasure ran,
    # the placeholders after it.
    subject: people.ErasureSubject | None
    emails: dict[uuid.UUID, str]


class ErasureService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    # --- reading ---------------------------------------------------------------------------

    async def list_requests(self, actor: Actor, *, limit: int) -> list[ErasureRecord]:
        async with self._db.unit_of_work() as session:
            await self._require(session, actor, "employee.erasure.read")
            found = await session.find(
                ErasureRequest, {}, sort=[("requested_at", -1), ("id", -1)], limit=limit
            )
            return [await self._record(session, request) for request in found]

    async def get(self, actor: Actor, request_id: uuid.UUID) -> ErasureRecord:
        async with self._db.unit_of_work() as session:
            await self._require(session, actor, "employee.erasure.read")
            request = await session.get(ErasureRequest, request_id)
            if request is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            return await self._record(session, request)

    # --- requesting ------------------------------------------------------------------------

    async def request(
        self, actor: Actor, context: RequestContext, *, employee_code: str, reason: str
    ) -> ErasureRecord:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._require(session, actor, "employee.erasure.request")
            subject = await people.erasure_subject_by_code(session, employee_code)
            if subject is None:
                raise _field("employee_code", "erasure.unknown_code", "No employee record has this code.")
            await self._check_subject(session, actor, subject, now)
            request = ErasureRequest(
                employee_id=subject.employee_id,
                reason=reason,
                requested_by=actor.user_id,
                requested_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(request)
            try:
                await session.flush()
            except UniqueViolationError as exc:
                if exc.index == "uq_employee_erasures_open":
                    raise _conflict(
                        "erasure.already_requested",
                        "An erasure of this person's data is already waiting or in progress.",
                    ) from exc
                raise
            await self._audit.record(
                session,
                AuditEvent(
                    action="people.erasure.requested",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="employee_erasure",
                    target_id=request.id,
                    subject_employee_id=subject.employee_id,
                    changes={"status": ValueChange(None, request.status)},
                    reason=reason,
                ),
            )
            return await self._record(session, request)

    async def cancel(self, actor: Actor, context: RequestContext, request_id: uuid.UUID) -> ErasureRecord:
        """The requester withdraws a request still waiting."""
        async with self._db.unit_of_work() as session:
            granted = await self._require(session, actor, "employee.erasure.request")
            request = await self._waiting(session, request_id)
            if request.requested_by != actor.user_id:
                raise AccessDenied(code="erasure.not_requester")
            return await self._close(
                session,
                actor=actor,
                context=context,
                granted=granted,
                request=request,
                status=ErasureStatus.CANCELLED,
                note=None,
            )

    async def reject(
        self, actor: Actor, context: RequestContext, request_id: uuid.UUID, note: str
    ) -> ErasureRecord:
        async with self._db.unit_of_work() as session:
            granted = await self._require(session, actor, "employee.erasure.approve")
            request = await self._waiting(session, request_id)
            self._refuse_own(actor, request)
            return await self._close(
                session,
                actor=actor,
                context=context,
                granted=granted,
                request=request,
                status=ErasureStatus.REJECTED,
                note=note,
            )

    # --- approving and running -------------------------------------------------------------

    async def approve(
        self,
        actor: Actor,
        context: RequestContext,
        request_id: uuid.UUID,
        *,
        employee_code: str,
        note: str | None,
    ) -> ErasureRecord:
        """Erase, in one transaction. A failure changes nothing and leaves the request waiting;
        a second approval of the same request finds it already decided (409)."""
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._require(session, actor, "employee.erasure.approve")
            request = await self._waiting(session, request_id)
            self._refuse_own(actor, request)
            subject = await people.erasure_subject(session, request.employee_id, lock=True)
            if subject is None:
                raise RuntimeError("an erasure request names a missing employee record")
            if employee_code.strip().casefold() != subject.employee_code.casefold():
                raise _field(
                    "employee_code",
                    "erasure.code_mismatch",
                    "This is not the employee code of the person named in the request.",
                )
            await self._check_subject(session, actor, subject, now)
            parts = await self._erase(session, actor, request.employee_id, now)
            keys = [key for part in parts for key in part.storage_keys]
            categories = [asdict(category) for part in parts for category in part.categories]
            request.decided_by = actor.user_id
            request.decided_at = now
            request.decision_note = note
            request.result = categories
            request.pending_storage_keys = keys
            request.files_total = len(keys)
            request.status = (ErasureStatus.FILES_PENDING if keys else ErasureStatus.COMPLETED).value
            request.open = bool(keys)
            request.completed_at = None if keys else now
            request.updated_at = now
            request.version += 1
            await session.flush()
            outcomes = Counter[str]()
            for category in categories:
                outcomes[category["outcome"]] += int(category["count"])
            await self._audit.record(
                session,
                AuditEvent(
                    action="people.employee.erased",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="employee_erasure",
                    target_id=request.id,
                    subject_employee_id=request.employee_id,
                    changes={
                        "status": ValueChange(ErasureStatus.PENDING.value, request.status),
                        "requested_by": ValueChange(None, request.requested_by),
                        **{
                            f"records_{outcome.value}": ValueChange(None, outcomes[outcome.value])
                            for outcome in Outcome
                        },
                    },
                    reason=note,
                ),
            )
            return await self._record(session, request)

    async def _erase(
        self, session: Session, actor: Actor, employee_id: uuid.UUID, now: datetime
    ) -> list[Erased]:
        """Every module's part, in the caller's transaction."""
        user_id, account = await identity.erase_account(session, employee_id, now)
        roles = await access.end_roles_for_erasure(session, user_id, by=actor.user_id, now=now)
        person = await people.erase_employee(session, employee_id, now)
        headed = await org.clear_department_head(session, employee_id)
        days = await attendance.erase_employee(session, employee_id)
        leaves = await leave.erase_employee(session, employee_id)
        files = await documents.erase_employee(session, employee_id, now)
        subjects = [*days.subject_ids, *leaves.subject_ids, *files.subject_ids]
        notices = await notify.erase_for(session, user_id, subjects)
        exports = await reports.exports_held(session, user_id)
        return [
            person,
            account,
            roles,
            headed,
            days,
            leaves,
            files,
            notices,
            exports,
            await _audit(session, employee_id, user_id),
        ]

    # --- helpers ---------------------------------------------------------------------------

    async def _require(self, session: Session, actor: Actor, permission: str) -> Granted:
        granted = await self._authz.require(session, actor, permission)
        if RoleKey.SUPER_ADMIN not in actor.role_keys:
            raise AccessDenied(code="erasure.super_admin_only")
        return granted

    async def _check_subject(
        self, session: Session, actor: Actor, subject: people.ErasureSubject, now: datetime
    ) -> None:
        if subject.employee_id == actor.employee_id:
            raise AccessDenied(code=SodRule.OWN_REQUEST.value)
        if subject.erased:
            raise _conflict("erasure.already_erased", "This person's data was already erased.")
        if (
            subject.status != "exited"
            or subject.date_of_exit is None
            or subject.date_of_exit >= self._authz.today()
        ):
            raise _conflict(
                "erasure.not_exited",
                "Offboard this person first. Data is erased only after their last working day has passed.",
            )
        linked = await identity.linked_account(session, subject.employee_id)
        if linked is not None:
            if linked.user_id == actor.user_id:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            if await access.AccountRoles().role_keys(session, linked.user_id, now) & ADMIN_ACCOUNT_ROLES:
                raise _conflict(
                    "erasure.admin_account",
                    "This person's account still holds the super admin or system admin role. Another "
                    "super admin removes it first; at least two active super admins must remain.",
                )

    @staticmethod
    def _refuse_own(actor: Actor, request: ErasureRequest) -> None:
        """Separation of duties: a request is decided by a super admin other than its requester."""
        if request.requested_by == actor.user_id:
            raise AccessDenied(code=SodRule.OWN_REQUEST.value)

    @staticmethod
    async def _waiting(session: Session, request_id: uuid.UUID) -> ErasureRequest:
        request = await session.get(ErasureRequest, request_id, for_update=True)
        if request is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        if request.status != ErasureStatus.PENDING:
            raise _conflict("erasure.already_decided", "This request was already decided or withdrawn.")
        return request

    async def _close(
        self,
        session: Session,
        *,
        actor: Actor,
        context: RequestContext,
        granted: Granted,
        request: ErasureRequest,
        status: ErasureStatus,
        note: str | None,
    ) -> ErasureRecord:
        now = self._clock.now()
        request.status = status.value
        request.open = False
        if status is ErasureStatus.REJECTED:
            request.decided_by = actor.user_id
            request.decided_at = now
            request.decision_note = note
        request.updated_at = now
        request.version += 1
        await session.flush()
        await self._audit.record(
            session,
            AuditEvent(
                action=f"people.erasure.{status.value}",
                actor=actor.audit_actor(),
                context=context,
                permission_used=granted.key,
                target_type="employee_erasure",
                target_id=request.id,
                subject_employee_id=request.employee_id,
                changes={"status": ValueChange(ErasureStatus.PENDING.value, status.value)},
                reason=note,
            ),
        )
        return await self._record(session, request)

    @staticmethod
    async def _record(session: Session, request: ErasureRequest) -> ErasureRecord:
        subject = await people.erasure_subject(session, request.employee_id)
        users = {request.requested_by} | ({request.decided_by} if request.decided_by else set())
        return ErasureRecord(request, subject, await identity.emails_of(session, users))


async def _audit(session: Session, employee_id: uuid.UUID, user_id: uuid.UUID | None) -> Erased:
    """Audit and security records are never changed (CLAUDE.md rule 19); they are reported."""
    result = Erased()
    rows = await session.count(AuditLogRecord, {"subject_employee_id": employee_id})
    if user_id is not None:
        rows += await session.count(
            AuditLogRecord, {"target_id": str(user_id), "subject_employee_id": {"$ne": employee_id}}
        )
    result.add(
        "audit.audit_log",
        Outcome.RETAINED,
        rows,
        "Audit records about the person or their account: never changed (CLAUDE.md rule 19). They hold IDs "
        "and changed field names, and any reasons typed with a change; they are removed by the audit "
        "retention job when their retention period ends.",
    )
    events = await session.count(SecurityEventRecord, {"user_id": user_id}) if user_id else 0
    result.add(
        "audit.security_events",
        Outcome.RETAINED,
        events,
        "Sign-ins and account security events with their addresses and browsers: kept until the "
        "security event retention period ends (rule 19).",
    )
    return result


def summarize(categories: Sequence[dict[str, Any]]) -> list[Category]:
    return [
        Category(str(c["key"]), Outcome(c["outcome"]), int(c["count"]), str(c["note"])) for c in categories
    ]
