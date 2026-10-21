"""Attendance corrections (product-requirements ATT-7, ATT-8, ATT-11; authorization-model
SOD-1, SOD-7).

- An employee proposes changes to one of their own work dates (events to add at stated times,
  events to void) with a reason (`attendance.correction.request.self`). One correction per
  employee and date can wait at a time. They may withdraw it while it waits.
- A manager (`attendance.correction.approve.team`) or HR (`.all`) decides it, never their own
  (SOD-1) and never one they submitted; a rejection needs a note. Approval appends the added
  events (source `correction`) and void rows for the removed ones: original events are never
  changed or deleted, and the day is judged again from the result at once.
- HR records a correction on an employee's behalf with a reason
  (`attendance.correction.create.all`): it applies at once and notifies the employee and their
  manager (SOD-7). HR's own attendance goes through the normal request.
- A work date inside an active attendance lock can be corrected only by someone holding
  `attendance.correction.approve.all` for the employee; such corrections are flagged
  (`locked_period`) and audited as such (ATT-11).
- Every change runs under the employee's attendance lock (the same one clocking takes), and
  is validated again when decided, so a correction never produces an impossible day.
- Reading follows `attendance.read` (self, team, all). The reason and the decision note are
  shown to the employee, to whoever may decide, and to HR who recorded it; such a read by
  someone else is audited. Lists never include them.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.modules.attendance import notifications
from app.modules.attendance.correction_rules import Add, CorrectionRuleError, Existing, resulting_day
from app.modules.attendance.days import voided_ids
from app.modules.attendance.models import (
    AttendanceCorrection,
    AttendanceCorrectionItem,
    AttendanceEvent,
    AttendanceEventVoid,
    AttendanceLock,
    CorrectionAction,
    CorrectionKind,
    CorrectionStatus,
    EventSource,
)
from app.modules.attendance.timeline import EventType
from app.modules.identity import public as identity
from app.modules.org import public as org
from app.modules.people import public as people
from app.platform.audit.records import AuditEvent, RedactedChange, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer
from app.platform.authz.sod import SodRule
from app.platform.clock import Clock
from app.platform.db import Database, Session, UniqueViolationError
from app.platform.errors import ProblemError, ProblemType
from app.platform.ratelimit import RateLimiter, RateLimitRule, RateLimitScope

SUBMISSIONS_PER_USER: Final = RateLimitRule(
    RateLimitScope.API, "attendance.corrections", 10, timedelta(minutes=1)
)
LOCKED_CODE: Final = "attendance.locked"


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def _own_employee(actor: Actor) -> uuid.UUID:
    if actor.employee_id is None:
        raise ProblemError(ProblemType.NOT_FOUND, detail="Your account is not linked to an employee record.")
    return actor.employee_id


@dataclass(frozen=True, slots=True)
class Proposal:
    work_date: date
    reason: str
    adds: tuple[Add, ...]
    voids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    event_type: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CorrectionRecord:
    correction: AttendanceCorrection
    employee_name: str


@dataclass(frozen=True, slots=True)
class CorrectionDetail:
    record: CorrectionRecord
    items: list[AttendanceCorrectionItem]
    # The day as it was before the correction, and as it is (or would be) with it.
    before: list[TimelineEvent]
    after: list[TimelineEvent]
    # For a waiting correction: whether it still fits the day as recorded now.
    still_valid: bool
    time_zone: str
    reason_visible: bool
    requested_by: str
    decided_by: str | None
    can_decide: bool
    can_cancel: bool


@dataclass(frozen=True, slots=True)
class CorrectionListing:
    records: list[CorrectionRecord]
    next_after: tuple[str, uuid.UUID] | None


@dataclass(frozen=True, slots=True)
class _DayState:
    zone_name: str
    existing: list[AttendanceEvent]
    voided: set[uuid.UUID]
    earlier_last: datetime | None
    later_first: datetime | None

    def effective(self) -> list[Existing]:
        return [
            Existing(event.id, EventType(event.event_type), event.occurred_at)
            for event in self.existing
            if event.id not in self.voided
        ]


async def _lock_employee(session: Session, employee_id: uuid.UUID) -> None:
    """The same lock clocking takes, so live events and corrections never interleave."""
    await session.lock(f"attendance:{employee_id}")


async def locked_on(session: Session, day: date) -> bool:
    return await session.exists(
        AttendanceLock, {"released_at": None, "period_start": {"$lte": day}, "period_end": {"$gte": day}}
    )


async def _zone(session: Session, employee_id: uuid.UUID, on: date) -> str:
    facts = await people.job_facts(session, employee_id, on)
    if facts is None or facts.location_id is None:
        return "UTC"
    zones = await org.location_time_zones(session, [facts.location_id])
    return zones.get(facts.location_id, "UTC")


async def _nearest_unvoided(
    session: Session, employee_id: uuid.UUID, work_dates: dict[str, date], *, latest: bool
) -> datetime | None:
    """The time of the latest (or earliest) event no correction voided on the given work dates."""
    pipeline = [
        {
            "$match": {
                "employee_id": employee_id,
                "work_date": {op: value.isoformat() for op, value in work_dates.items()},
            }
        },
        session.tenant_lookup(AttendanceEventVoid, local="_id", foreign="event_id", into="void"),
        {"$match": {"void": {"$size": 0}}},
        {"$sort": {"occurred_at": -1 if latest else 1}},
        {"$limit": 1},
    ]
    found = await session.aggregate(AttendanceEvent, pipeline)
    return found[0]["occurred_at"] if found else None


async def _day_state(session: Session, employee_id: uuid.UUID, work_date: date) -> _DayState:
    rows = await session.find(
        AttendanceEvent,
        {"employee_id": employee_id, "work_date": work_date},
        sort=[("occurred_at", 1), ("id", 1)],
    )
    earlier_last = await _nearest_unvoided(session, employee_id, {"$lt": work_date}, latest=True)
    later_first = await _nearest_unvoided(session, employee_id, {"$gt": work_date}, latest=False)
    return _DayState(
        zone_name=await _zone(session, employee_id, work_date),
        existing=rows,
        voided=await voided_ids(session, [row.id for row in rows]),
        earlier_last=earlier_last,
        later_first=later_first,
    )


def _validate(
    state: _DayState, adds: Sequence[Add], voids: Sequence[uuid.UUID], work_date: date, now: datetime
) -> list[TimelineEvent]:
    try:
        events = resulting_day(
            state.effective(),
            adds,
            voids,
            work_date=work_date,
            zone=ZoneInfo(state.zone_name),
            earlier_last=state.earlier_last,
            later_first=state.later_first,
            now=now,
        )
    except CorrectionRuleError as error:
        raise field_error(error.field, error.code, error.message) from error
    return [TimelineEvent(event.event_type.value, event.occurred_at) for event in events]


def _adds_and_voids(items: Sequence[AttendanceCorrectionItem]) -> tuple[list[Add], list[uuid.UUID]]:
    adds = [
        Add(EventType(item.event_type), item.proposed_at)
        for item in items
        if item.action == CorrectionAction.ADD and item.event_type and item.proposed_at
    ]
    voids = [
        item.target_event_id
        for item in items
        if item.action == CorrectionAction.VOID and item.target_event_id
    ]
    return adds, voids


class CorrectionService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        audit: AuditWriter,
        authorizer: Authorizer,
        rate_limiter: RateLimiter,
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer
        self._limiter = rate_limiter

    # --- creating ------------------------------------------------------------------------------

    async def submit(
        self, actor: Actor, context: RequestContext, proposal: Proposal, idempotency_key: str
    ) -> CorrectionDetail:
        employee_id = _own_employee(actor)
        await self._limiter.enforce(SUBMISSIONS_PER_USER, str(actor.user_id))
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, "attendance.correction.request.self", subject_employee_id=employee_id
            )
            return await self._create(
                session,
                actor,
                context,
                employee_id=employee_id,
                proposal=proposal,
                idempotency_key=idempotency_key,
                kind=CorrectionKind.REQUEST,
                permission=granted.key,
            )

    async def record_for(
        self,
        actor: Actor,
        context: RequestContext,
        employee_id: uuid.UUID,
        proposal: Proposal,
        idempotency_key: str,
    ) -> CorrectionDetail:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session,
                actor,
                "attendance.correction.create.all",
                subject_employee_id=employee_id,
                hide_existence=True,
            )
            if actor.employee_id == employee_id:
                # SOD-7: HR's own attendance goes through the normal request and approval.
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            return await self._create(
                session,
                actor,
                context,
                employee_id=employee_id,
                proposal=proposal,
                idempotency_key=idempotency_key,
                kind=CorrectionKind.HR_RECORDED,
                permission=granted.key,
            )

    async def _create(
        self,
        session: Session,
        actor: Actor,
        context: RequestContext,
        *,
        employee_id: uuid.UUID,
        proposal: Proposal,
        idempotency_key: str,
        kind: CorrectionKind,
        permission: str,
    ) -> CorrectionDetail:
        now = self._clock.now()
        await _lock_employee(session, employee_id)
        replay = await session.find_one(
            AttendanceCorrection, {"employee_id": employee_id, "idempotency_key": idempotency_key}
        )
        if replay is not None:
            if replay.work_date != proposal.work_date or replay.kind != kind.value:
                raise field_error(
                    "Idempotency-Key",
                    "idempotency.key_reused",
                    "This request key was already used for a different correction. Reload and try again.",
                )
            return await self._detail(session, actor, replay.id)
        facts = await people.job_facts(session, employee_id, proposal.work_date)
        if facts is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        state = await _day_state(session, employee_id, proposal.work_date)
        local_today = now.astimezone(ZoneInfo(state.zone_name)).date()
        if proposal.work_date > local_today:
            raise field_error(
                "work_date", "correction.future_date", "Choose a date that has already started."
            )
        if not facts.employed_on(proposal.work_date):
            raise field_error(
                "work_date",
                "correction.not_employed",
                "Choose a date between the joining date and the last working day.",
            )
        locked = await locked_on(session, proposal.work_date)
        if locked and kind is CorrectionKind.HR_RECORDED:
            await self._require_locked_access(session, actor, employee_id)
        _validate(state, proposal.adds, proposal.voids, proposal.work_date, now)
        hr = kind is CorrectionKind.HR_RECORDED
        correction = AttendanceCorrection(
            employee_id=employee_id,
            work_date=proposal.work_date,
            kind=kind.value,
            reason=proposal.reason,
            status=(CorrectionStatus.APPROVED if hr else CorrectionStatus.PENDING).value,
            requested_by=actor.user_id,
            decided_by=actor.user_id if hr else None,
            decided_at=now if hr else None,
            locked_period=locked,
            idempotency_key=idempotency_key,
        )
        pending_exists = conflict(
            "correction.pending_exists",
            "A correction for this date is already waiting for a decision. Withdraw it or wait for it first.",
        )
        if not hr and await session.exists(
            AttendanceCorrection,
            {
                "employee_id": employee_id,
                "work_date": proposal.work_date,
                "status": CorrectionStatus.PENDING.value,
            },
        ):
            raise pending_exists
        session.add(correction)
        try:
            await session.flush()
        except UniqueViolationError as exc:
            raise pending_exists from exc
        items = [
            AttendanceCorrectionItem(
                correction_id=correction.id,
                action=CorrectionAction.ADD.value,
                event_type=add.event_type.value,
                proposed_at=add.proposed_at,
            )
            for add in proposal.adds
        ] + [
            AttendanceCorrectionItem(
                correction_id=correction.id, action=CorrectionAction.VOID.value, target_event_id=target
            )
            for target in proposal.voids
        ]
        session.add_all(items)
        await session.flush()
        if hr:
            await self._apply(session, correction, items, user_id=actor.user_id)
        await self._audit.record(
            session,
            AuditEvent(
                action="attendance.correction.recorded" if hr else "attendance.correction.submitted",
                actor=actor.audit_actor(),
                context=context,
                permission_used=permission,
                target_type="attendance_correction",
                target_id=correction.id,
                subject_employee_id=employee_id,
                changes={
                    "work_date": ValueChange(None, proposal.work_date),
                    "status": ValueChange(None, correction.status),
                    "events_added": ValueChange(None, len(proposal.adds)),
                    "events_voided": ValueChange(None, len(proposal.voids)),
                    "locked_period": ValueChange(None, locked),
                    "reason": RedactedChange(),
                },
            ),
        )
        today = self._authz.today()
        if hr:
            await notifications.to_employee(session, correction, "recorded", now)
            await notifications.to_manager(session, correction, "recorded_for_team", now=now, today=today)
        else:
            await notifications.to_manager(session, correction, "submitted", now=now, today=today)
        return await self._detail(session, actor, correction.id)

    async def _apply(
        self,
        session: Session,
        correction: AttendanceCorrection,
        items: Sequence[AttendanceCorrectionItem],
        *,
        user_id: uuid.UUID,
    ) -> None:
        """Append the correction's events and voids. Original events stay as they are."""
        for number, item in enumerate(items, start=1):
            if item.action == CorrectionAction.ADD and item.event_type and item.proposed_at:
                session.add(
                    AttendanceEvent(
                        employee_id=correction.employee_id,
                        event_type=item.event_type,
                        occurred_at=item.proposed_at,
                        work_date=correction.work_date,
                        source=EventSource.CORRECTION.value,
                        recorded_by=user_id,
                        idempotency_key=f"correction:{correction.id}:{number}",
                        correction_id=correction.id,
                    )
                )
            elif item.target_event_id is not None:
                session.add(
                    AttendanceEventVoid(
                        event_id=item.target_event_id, correction_id=correction.id, voided_by=user_id
                    )
                )
        await session.flush()

    async def _require_locked_access(self, session: Session, actor: Actor, employee_id: uuid.UUID) -> None:
        """ATT-11: a locked date needs the `all` scope over the employee."""
        if not actor.holds("attendance.correction.approve.all"):
            raise AccessDenied(code=LOCKED_CODE)
        try:
            await self._authz.require(
                session, actor, "attendance.correction.approve.all", subject_employee_id=employee_id
            )
        except AccessDenied as exc:
            raise AccessDenied(code=LOCKED_CODE) from exc

    # --- withdrawing and deciding ----------------------------------------------------------------

    async def cancel(
        self, actor: Actor, context: RequestContext, correction_id: uuid.UUID
    ) -> CorrectionDetail:
        employee_id = _own_employee(actor)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, "attendance.correction.request.self", subject_employee_id=employee_id
            )
            await _lock_employee(session, employee_id)
            correction = await session.get(AttendanceCorrection, correction_id, for_update=True)
            if correction is None or correction.employee_id != employee_id:
                raise ProblemError(ProblemType.NOT_FOUND)
            if correction.status != CorrectionStatus.PENDING:
                raise conflict(
                    "correction.not_pending", "This correction has already been decided or withdrawn."
                )
            correction.status = CorrectionStatus.CANCELLED.value
            correction.version += 1
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.correction.cancelled",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_correction",
                    target_id=correction.id,
                    subject_employee_id=employee_id,
                    changes={"status": ValueChange(CorrectionStatus.PENDING.value, correction.status)},
                ),
            )
            return await self._detail(session, actor, correction.id)

    async def decide(
        self,
        actor: Actor,
        context: RequestContext,
        correction_id: uuid.UUID,
        *,
        approve: bool,
        note: str | None,
    ) -> CorrectionDetail:
        if not approve and note is None:
            raise field_error(
                "note", "correction.note_required", "Say why, so the employee knows what to do next."
            )
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            found = await session.get(AttendanceCorrection, correction_id)
            if found is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            subject = found.employee_id
            granted = await self._authz.require(
                session,
                actor,
                "attendance.correction.approve",
                subject_employee_id=subject,
                hide_existence=True,
            )
            if actor.employee_id == subject:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            await _lock_employee(session, subject)
            correction = await session.get(AttendanceCorrection, correction_id, for_update=True)
            if correction is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if correction.requested_by == actor.user_id:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            if correction.status != CorrectionStatus.PENDING:
                raise conflict(
                    "correction.already_decided",
                    "This correction has already been decided or withdrawn. Reload to see its current state.",
                )
            locked = await locked_on(session, correction.work_date)
            if locked:
                await self._require_locked_access(session, actor, subject)
            items = await session.find(
                AttendanceCorrectionItem, {"correction_id": correction.id}, sort=[("id", 1)]
            )
            if approve:
                adds, voids = _adds_and_voids(items)
                state = await _day_state(session, subject, correction.work_date)
                _validate(state, adds, voids, correction.work_date, now)
                await self._apply(session, correction, items, user_id=actor.user_id)
            correction.status = (CorrectionStatus.APPROVED if approve else CorrectionStatus.REJECTED).value
            correction.decided_by = actor.user_id
            correction.decided_at = now
            correction.decision_note = note
            correction.version += 1
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action=f"attendance.correction.{correction.status}",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_correction",
                    target_id=correction.id,
                    subject_employee_id=subject,
                    changes={
                        "status": ValueChange(CorrectionStatus.PENDING.value, correction.status),
                        "locked_period": ValueChange(None, locked),
                        **({"note": RedactedChange()} if note else {}),
                    },
                ),
            )
            await notifications.to_employee(session, correction, correction.status, now)
            return await self._detail(session, actor, correction.id)

    # --- reading -------------------------------------------------------------------------------

    async def detail(
        self, actor: Actor, context: RequestContext, correction_id: uuid.UUID
    ) -> CorrectionDetail:
        async with self._db.unit_of_work() as session:
            found = await session.get(AttendanceCorrection, correction_id)
            if found is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            subject = found.employee_id
            await self._authz.require(
                session, actor, "attendance.read", subject_employee_id=subject, hide_existence=True
            )
            detail = await self._detail(session, actor, correction_id)
            if detail.reason_visible and actor.employee_id != subject:
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="attendance.correction.reason_read",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used="attendance.correction.approve",
                        target_type="attendance_correction",
                        target_id=correction_id,
                        subject_employee_id=subject,
                    ),
                )
            return detail

    async def _may_decide(self, session: Session, actor: Actor, correction: AttendanceCorrection) -> bool:
        if (
            actor.employee_id == correction.employee_id
            or correction.requested_by == actor.user_id
            or not self._authz.holds_any(actor, "attendance.correction.approve")
        ):
            return False
        try:
            await self._authz.require(
                session, actor, "attendance.correction.approve", subject_employee_id=correction.employee_id
            )
        except AccessDenied:
            return False
        return True

    async def _detail(self, session: Session, actor: Actor, correction_id: uuid.UUID) -> CorrectionDetail:
        correction = await session.get(AttendanceCorrection, correction_id)
        if correction is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        own = actor.employee_id == correction.employee_id
        may_decide = await self._may_decide(session, actor, correction)
        items = await session.find(
            AttendanceCorrectionItem, {"correction_id": correction.id}, sort=[("id", 1)]
        )
        state = await _day_state(session, correction.employee_id, correction.work_date)
        void_owner = (
            {
                void.event_id: void.correction_id
                for void in await session.find(
                    AttendanceEventVoid, {"event_id": {"$in": [e.id for e in state.existing]}}
                )
            }
            if state.existing
            else {}
        )

        def line(events: Sequence[AttendanceEvent]) -> list[TimelineEvent]:
            return [TimelineEvent(e.event_type, e.occurred_at) for e in events]

        current = [e for e in state.existing if e.id not in state.voided]
        still_valid = True
        if correction.status == CorrectionStatus.APPROVED:
            before = line(
                [
                    e
                    for e in state.existing
                    if e.correction_id != correction.id
                    and (e.id not in state.voided or void_owner.get(e.id) == correction.id)
                ]
            )
            after = line(current)
        else:
            before = line(current)
            adds, voids = _adds_and_voids(items)
            removed = set(voids)
            proposed = [
                TimelineEvent(e.event_type, e.occurred_at) for e in current if e.id not in removed
            ] + [TimelineEvent(add.event_type.value, add.proposed_at) for add in adds]
            after = sorted(proposed, key=lambda event: event.occurred_at)
            if correction.status == CorrectionStatus.PENDING:
                try:
                    _validate(state, adds, voids, correction.work_date, self._clock.now())
                except ProblemError:
                    still_valid = False
        people_names = await identity.emails_of(
            session, {correction.requested_by} | ({correction.decided_by} if correction.decided_by else set())
        )
        recorded_by_actor = correction.requested_by == actor.user_id
        return CorrectionDetail(
            record=(await self._records(session, [correction]))[0],
            items=items,
            before=before,
            after=after,
            still_valid=still_valid,
            time_zone=state.zone_name,
            reason_visible=own or may_decide or recorded_by_actor,
            requested_by=people_names.get(correction.requested_by, "Someone"),
            decided_by=people_names.get(correction.decided_by, "An approver")
            if correction.decided_by
            else None,
            can_decide=may_decide and correction.status == CorrectionStatus.PENDING,
            can_cancel=own and correction.status == CorrectionStatus.PENDING,
        )

    @staticmethod
    async def _records(session: Session, rows: Sequence[AttendanceCorrection]) -> list[CorrectionRecord]:
        names = await people.employee_names(session, {row.employee_id for row in rows})
        return [CorrectionRecord(row, names.get(row.employee_id, "")) for row in rows]

    async def corrections(
        self,
        actor: Actor,
        *,
        own: bool,
        for_decision: bool,
        statuses: tuple[str, ...],
        employee_id: uuid.UUID | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> CorrectionListing:
        """Corrections in the actor's scope, newest work date first; their own; or, with
        `for_decision`, those waiting for a decision they may make (oldest first)."""
        async with self._db.unit_of_work() as session:
            conditions: list[dict[str, Any]] = []
            if own:
                conditions.append({"employee_id": _own_employee(actor)})
            elif for_decision:
                conditions.append(
                    await self._authz.scope_filter(
                        session, actor, "attendance.correction.approve", "employee_id"
                    )
                )
                conditions.append(
                    {"status": CorrectionStatus.PENDING.value, "requested_by": {"$ne": actor.user_id}}
                )
                if actor.employee_id:
                    conditions.append({"employee_id": {"$ne": actor.employee_id}})
            else:
                conditions.append(
                    await self._authz.scope_filter(session, actor, "attendance.read", "employee_id")
                )
            if statuses:
                conditions.append({"status": {"$in": list(statuses)}})
            if employee_id is not None:
                conditions.append({"employee_id": employee_id})
            if after is not None:
                beyond = "$gt" if for_decision else "$lt"
                bound = date.fromisoformat(after[0])
                conditions.append(
                    {"$or": [{"work_date": {beyond: bound}}, {"work_date": bound, "id": {beyond: after[1]}}]}
                )
            direction = 1 if for_decision else -1
            rows = await session.find(
                AttendanceCorrection,
                {"$and": [condition for condition in conditions if condition]} if any(conditions) else {},
                sort=[("work_date", direction), ("id", direction)],
                limit=limit + 1,
            )
            records = await self._records(session, rows[:limit])
        last = rows[limit - 1] if len(rows) > limit else None
        return CorrectionListing(records, (last.work_date.isoformat(), last.id) if last else None)
