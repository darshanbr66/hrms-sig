"""Office location verification (ADR-031; docs/database-design.md §4.5): which offices a person
may clock at, the evidence a clock event leaves, office checks and approvals, and HR review.

- **Offices a person may clock at** on a work date: the location of their job on that date,
  plus the offices approved for them on that date (`OfficeApproval`). Only active locations
  count, and only those with a check version in effect whose mode is `verify`. Everything is
  read from the person's own records in their organization; the client never names an office
  or a person.
- **Evidence** is written by the clock service in the event's transaction, under the person's
  attendance lock (`record_evidence`), so a replayed request (which returns before) never
  writes a second one, and the unique index on the event backs that up. The position the
  browser reported is used here and discarded: no coordinates are stored or logged.
- **The installation switch** (`LOCATION_CHECKS_ENABLED`, off by default): while off, no check
  can be created or versioned (503 `location.checks_disabled`) and the clock service applies
  none, whatever is stored. No role or permission can override it.
- **Checks** (`attendance.policy.manage`) and **approvals** (`attendance.location.assign.all`,
  within the actor's scope) are effective-dated, start today or later, never rewrite a version
  that started, and are audited.
- **Reviews** (`attendance.location.review.all`, within the reviewer's scope, never of their
  own evidence) are append-only and audited; each names the review it replaces, so a decision
  made on a stale view is refused (409). A review never changes attendance: changes go through
  the correction workflow. Reading one piece of evidence in detail is audited too.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from app.modules.attendance import location_rules as rules
from app.modules.attendance.config import supersede
from app.modules.attendance.days import voided_ids
from app.modules.attendance.models import (
    REVIEW_REASONS,
    AttendanceEvent,
    CheckMode,
    LocationCheck,
    LocationEvidence,
    LocationReview,
    OfficeApproval,
)
from app.modules.attendance.schedule import overlapping
from app.modules.identity import public as identity
from app.modules.org import public as org
from app.modules.people import public as people
from app.platform.audit.records import AuditEvent, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer
from app.platform.authz.sod import SodRule
from app.platform.clock import Clock
from app.platform.db import Database, Session, UniqueViolationError
from app.platform.errors import ProblemError, ProblemType

REVIEW: Final = "attendance.location.review"
ASSIGN: Final = "attendance.location.assign"
CHECK_FIELDS: Final = (
    "mode",
    "latitude",
    "longitude",
    "radius_m",
    "max_accuracy_m",
    "max_position_age_seconds",
    "review_unverified",
)


def checks_disabled() -> ProblemError:
    return ProblemError(
        ProblemType.TEMPORARILY_UNAVAILABLE,
        detail="Office location checks are turned off for this installation. They are turned on "
        "only after the retention period and the privacy notice are approved.",
        extensions={"code": "location.checks_disabled"},
    )


def _audited(value: Any) -> Any:
    """Audit values are exact: an office's coordinates go in as decimals."""
    return Decimal(str(value)) if isinstance(value, float) else value


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


# --- offices and evidence (used by the clock service) ---------------------------------------------


async def eligible_offices(session: Session, employee_id: uuid.UUID, on: date) -> list[rules.Office]:
    """The offices with a check the person may clock at on `on` (see the module docstring)."""
    placement = await people.placement(session, employee_id, on)
    approvals = await session.find(OfficeApproval, {"employee_id": employee_id, **overlapping(on, on)})
    location_ids = {approval.location_id for approval in approvals}
    if placement is not None:
        location_ids.add(placement.location_id)
    active = await org.active_locations(session, location_ids)
    if not active:
        return []
    checks = await session.find(
        LocationCheck, {"location_id": {"$in": sorted(active)}, **overlapping(on, on)}
    )
    return [
        rules.Office(
            check_id=check.id,
            location_id=check.location_id,
            code=active[check.location_id][0],
            latitude=check.latitude,
            longitude=check.longitude,
            radius_m=check.radius_m,
            max_accuracy_m=check.max_accuracy_m,
            max_position_age_seconds=check.max_position_age_seconds,
            review_unverified=check.review_unverified,
            placed=placement is not None and check.location_id == placement.location_id,
        )
        for check in checks
        if check.mode == CheckMode.VERIFY
    ]


async def record_evidence(
    session: Session,
    event: AttendanceEvent,
    report: rules.Position | rules.Reason | None,
    now: datetime,
) -> LocationEvidence | None:
    """Judge the reported position for a new clock event and store the result; nothing when no
    office the person may clock at has a check that day (a position sent anyway is ignored)."""
    offices = await eligible_offices(session, event.employee_id, event.work_date)
    verdict = rules.judge(offices, report)
    if verdict is None:
        return None
    evidence = LocationEvidence(
        event_id=event.id,
        employee_id=event.employee_id,
        work_date=event.work_date,
        event_type=event.event_type,
        location_id=verdict.office.location_id,
        check_id=verdict.office.check_id,
        radius_m=verdict.office.radius_m,
        max_accuracy_m=verdict.office.max_accuracy_m,
        offices_considered=len(offices),
        outcome=verdict.outcome.value,
        reason_codes=[reason.value for reason in verdict.reasons],
        distance_m=verdict.distance_m,
        distance_over_cap=verdict.distance_over_cap,
        accuracy_m=verdict.accuracy_m,
        position_age_seconds=verdict.position_age_seconds,
        review_required=verdict.review_required,
        received_at=now,
    )
    session.add(evidence)
    return evidence


@dataclass(frozen=True, slots=True)
class EventLocation:
    outcome: str
    reason_codes: tuple[str, ...]


async def outcomes_of(session: Session, event_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, EventLocation]:
    """The outcome of each event that has evidence (never distances), for views the caller has
    already decided the reader may see."""
    if not event_ids:
        return {}
    found = await session.find(LocationEvidence, {"event_id": {"$in": list(event_ids)}})
    return {e.event_id: EventLocation(e.outcome, tuple(e.reason_codes)) for e in found}


async def may_see_outcomes(session: Session, authz: Authorizer, actor: Actor, subject: uuid.UUID) -> bool:
    """The employee themselves, or a reviewer whose scope covers them. Managers and other
    attendance readers do not see location outcomes."""
    if actor.employee_id == subject:
        return True
    if not authz.holds_any(actor, REVIEW):
        return False
    try:
        await authz.require(session, actor, REVIEW, subject_employee_id=subject)
    except AccessDenied:
        return False
    return True


# --- records for the API ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckRecord:
    check: LocationCheck
    location_code: str
    location_name: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval: OfficeApproval
    employee_name: str
    employee_code: str
    location_code: str
    location_name: str


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    review: LocationReview
    reviewer: str | None


@dataclass(frozen=True, slots=True)
class QueueItem:
    evidence: LocationEvidence
    employee_name: str
    employee_code: str
    location_name: str
    event_occurred_at: datetime | None
    event_voided: bool
    current: LocationReview | None


@dataclass(frozen=True, slots=True)
class Queue:
    items: list[QueueItem]
    next_after: tuple[str, uuid.UUID] | None


@dataclass(frozen=True, slots=True)
class DayEventRecord:
    event: AttendanceEvent
    voided: bool


@dataclass(frozen=True, slots=True)
class EvidenceDetail:
    item: QueueItem
    day_events: list[DayEventRecord]
    reviews: list[ReviewRecord]
    can_review: bool


class LocationService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        audit: AuditWriter,
        authorizer: Authorizer,
        checks_enabled: bool,
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer
        # `LOCATION_CHECKS_ENABLED`: an installation setting, never a permission.
        self._checks_enabled = checks_enabled

    @property
    def checks_enabled(self) -> bool:
        return self._checks_enabled

    # --- office checks ---------------------------------------------------------------------

    async def checks(self, actor: Actor) -> list[CheckRecord]:
        async with self._db.unit_of_work() as session:
            await self._authz.require(session, actor, "shift.read")
            rows = await session.find(LocationCheck, {}, sort=[("location_id", 1), ("effective_from", -1)])
            names = await org.unit_names(session, location_ids={row.location_id for row in rows})
            codes = await org.active_locations(session, {row.location_id for row in rows})
            return [
                CheckRecord(
                    row,
                    codes.get(row.location_id, ("", ""))[0],
                    names.locations.get(row.location_id, "Unknown location"),
                )
                for row in rows
            ]

    async def create_check(
        self, actor: Actor, context: RequestContext, values: Mapping[str, Any]
    ) -> CheckRecord:
        """A new version of an office's check from `effective_from` (today or later); it closes
        the open one. Nothing is assumed: every value is the administrator's."""
        today = self._authz.today()
        location_id: uuid.UUID = values["location_id"]
        effective_from: date = values["effective_from"]
        if effective_from < today:
            raise _field(
                "effective_from",
                "location.check_in_past",
                "Start the check today or later: past clock events were judged with the settings then.",
            )
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "attendance.policy.manage")
            # After the permission, so who may not configure learns only that; before any write,
            # so no version (on or off) is stored while the installation has checks off.
            if not self._checks_enabled:
                raise checks_disabled()
            active = await org.active_locations(session, {location_id})
            if location_id not in active:
                raise _field("location_id", "location.unknown_office", "Choose an active location.")
            await session.lock(f"attendance_location_check:{location_id}")
            replaced = await supersede(
                session, LocationCheck, {"location_id": location_id}, effective_from, "office check"
            )
            check = LocationCheck(
                location_id=location_id,
                effective_from=effective_from,
                created_by=actor.user_id,
                **{name: values[name] for name in CHECK_FIELDS},
            )
            session.add(check)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.location_check.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_location_check",
                    target_id=check.id,
                    changes={
                        "location_id": ValueChange(None, location_id),
                        "effective_from": ValueChange(None, effective_from),
                        "replaces": ValueChange(None, replaced),
                        # The office's position and limits: organization configuration.
                        **{name: ValueChange(None, _audited(values[name])) for name in CHECK_FIELDS},
                    },
                ),
            )
            code, name = active[location_id]
            return CheckRecord(check, code, name)

    # --- office approvals ------------------------------------------------------------------

    async def approvals(
        self, actor: Actor, *, location_id: uuid.UUID | None, employee_id: uuid.UUID | None
    ) -> list[ApprovalRecord]:
        """Approvals the actor may see (their assignment scope), current and past."""
        async with self._db.unit_of_work() as session:
            scope = await self._authz.scope_filter(session, actor, ASSIGN, "employee_id")
            query: dict[str, Any] = dict(scope)
            if location_id is not None:
                query["location_id"] = location_id
            if employee_id is not None:
                query = {"$and": [query, {"employee_id": employee_id}]}
            rows = await session.find(
                OfficeApproval, query, sort=[("effective_from", -1), ("id", -1)], limit=500
            )
            return await self._approval_records(session, rows)

    async def approve(
        self,
        actor: Actor,
        context: RequestContext,
        *,
        employee_id: uuid.UUID,
        location_id: uuid.UUID,
        effective_from: date,
    ) -> ApprovalRecord:
        if effective_from < self._authz.today():
            raise _field("effective_from", "location.approval_in_past", "Start the approval today or later.")
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, ASSIGN, subject_employee_id=employee_id, hide_existence=True
            )
            if not await people.employee_exists(session, employee_id):
                raise ProblemError(ProblemType.NOT_FOUND)
            if location_id not in await org.active_locations(session, {location_id}):
                raise _field("location_id", "location.unknown_office", "Choose an active location.")
            await session.lock(f"attendance_office_approval:{employee_id}:{location_id}")
            series = {"employee_id": employee_id, "location_id": location_id}
            if await session.exists(
                OfficeApproval,
                {**series, "$or": [{"effective_to": None}, {"effective_to": {"$gt": effective_from}}]},
            ):
                raise _conflict(
                    "location.already_approved",
                    "This person is already approved for this office on that date. End that approval first.",
                )
            approval = OfficeApproval(
                employee_id=employee_id,
                location_id=location_id,
                effective_from=effective_from,
                created_by=actor.user_id,
            )
            session.add(approval)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.office_approval.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_office_approval",
                    target_id=approval.id,
                    subject_employee_id=employee_id,
                    changes={
                        "location_id": ValueChange(None, location_id),
                        "effective_from": ValueChange(None, effective_from),
                    },
                ),
            )
            return (await self._approval_records(session, [approval]))[0]

    async def end_approval(
        self, actor: Actor, context: RequestContext, approval_id: uuid.UUID, effective_to: date
    ) -> ApprovalRecord:
        """End an approval from a date (today or later); past days keep it."""
        async with self._db.unit_of_work() as session:
            approval = await session.get(OfficeApproval, approval_id, for_update=True)
            if approval is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            granted = await self._authz.require(
                session, actor, ASSIGN, subject_employee_id=approval.employee_id, hide_existence=True
            )
            if approval.effective_to is not None:
                raise _conflict("location.approval_ended", "This approval has already ended.")
            if effective_to < self._authz.today() or effective_to <= approval.effective_from:
                raise _field(
                    "effective_to",
                    "location.approval_end",
                    "End the approval today or later, after the day it started.",
                )
            approval.effective_to = effective_to
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.office_approval.ended",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_office_approval",
                    target_id=approval.id,
                    subject_employee_id=approval.employee_id,
                    changes={"effective_to": ValueChange(None, effective_to)},
                ),
            )
            return (await self._approval_records(session, [approval]))[0]

    @staticmethod
    async def _approval_records(session: Session, rows: Sequence[OfficeApproval]) -> list[ApprovalRecord]:
        refs = await people.employee_refs(session, {row.employee_id for row in rows})
        names = await org.unit_names(session, location_ids={row.location_id for row in rows})
        codes = await org.active_locations(session, {row.location_id for row in rows})
        return [
            ApprovalRecord(
                row,
                refs.get(row.employee_id, ("Unknown person", ""))[0],
                refs.get(row.employee_id, ("Unknown person", ""))[1],
                codes.get(row.location_id, ("", ""))[0],
                names.locations.get(row.location_id, "Unknown location"),
            )
            for row in rows
        ]

    # --- review ----------------------------------------------------------------------------

    async def queue(
        self,
        actor: Actor,
        *,
        status: str,
        outcome: str | None,
        location_id: uuid.UUID | None,
        start: date | None,
        end: date | None,
        text: str | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> Queue:
        """Evidence flagged for review, newest first, within the reviewer's scope and never the
        reviewer's own. `status`: `open` (no decision yet), `reviewed` or `all`."""
        async with self._db.unit_of_work() as session:
            scope = await self._authz.scope_filter(session, actor, REVIEW, "employee_id")
            conditions: list[Mapping[str, Any]] = [scope, {"review_required": True}]
            if actor.employee_id is not None:
                conditions.append({"employee_id": {"$ne": actor.employee_id}})
            if status != "all":
                reviewed = await session.distinct(LocationReview, "evidence_id", {})
                conditions.append({"id": {"$nin" if status == "open" else "$in": reviewed}})
            if outcome is not None:
                conditions.append({"outcome": outcome})
            if location_id is not None:
                conditions.append({"location_id": location_id})
            if start is not None:
                conditions.append({"work_date": {"$gte": start}})
            if end is not None:
                conditions.append({"work_date": {"$lte": end}})
            if text:
                conditions.append({"employee_id": {"$in": await people.ids_matching(session, text)}})
            if after is not None:
                received = datetime.fromisoformat(after[0])
                conditions.append(
                    {
                        "$or": [
                            {"received_at": {"$lt": received}},
                            {"received_at": received, "id": {"$lt": after[1]}},
                        ]
                    }
                )
            rows = await session.find(
                LocationEvidence,
                {"$and": conditions},
                sort=[("received_at", -1), ("id", -1)],
                limit=limit + 1,
            )
            page = rows[:limit]
            items = await self._items(session, page)
            last = page[-1] if page else None
            next_after = (
                (last.received_at.isoformat(), last.id) if last is not None and len(rows) > limit else None
            )
            return Queue(items, next_after)

    async def detail(self, actor: Actor, context: RequestContext, evidence_id: uuid.UUID) -> EvidenceDetail:
        """One piece of evidence with the day's events and its review history. Audited."""
        async with self._db.unit_of_work() as session:
            evidence = await session.get(LocationEvidence, evidence_id)
            if evidence is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            granted = await self._authz.require(
                session, actor, REVIEW, subject_employee_id=evidence.employee_id, hide_existence=True
            )
            if actor.employee_id == evidence.employee_id:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            [item] = await self._items(session, [evidence])
            events = await session.find(
                AttendanceEvent,
                {"employee_id": evidence.employee_id, "work_date": evidence.work_date},
                sort=[("occurred_at", 1), ("id", 1)],
            )
            voided = await voided_ids(session, [event.id for event in events])
            reviews = await session.find(
                LocationReview, {"evidence_id": evidence.id}, sort=[("reviewed_at", 1)]
            )
            reviewers = await identity.emails_of(session, {review.reviewed_by for review in reviews})
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.location.evidence_read",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_location_evidence",
                    target_id=evidence.id,
                    subject_employee_id=evidence.employee_id,
                ),
            )
            return EvidenceDetail(
                item=item,
                day_events=[DayEventRecord(event, event.id in voided) for event in events],
                reviews=[ReviewRecord(review, reviewers.get(review.reviewed_by)) for review in reviews],
                can_review=evidence.review_required,
            )

    async def decide(
        self,
        actor: Actor,
        context: RequestContext,
        evidence_id: uuid.UUID,
        *,
        decision: str,
        reason_code: str,
        note: str | None,
        supersedes: uuid.UUID | None,
    ) -> LocationReview:
        """Record a decision. It never changes the attendance event."""
        if decision not in REVIEW_REASONS.get(reason_code, frozenset()):
            raise _field("reason_code", "location.review_reason", "Choose a reason that fits the decision.")
        async with self._db.unit_of_work() as session:
            evidence = await session.get(LocationEvidence, evidence_id)
            if evidence is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            granted = await self._authz.require(
                session, actor, REVIEW, subject_employee_id=evidence.employee_id, hide_existence=True
            )
            if actor.employee_id == evidence.employee_id:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            if not evidence.review_required:
                raise _conflict(
                    "location.review_not_needed", "This clock event was verified; there is nothing to review."
                )
            await session.lock(f"attendance_location_review:{evidence.id}")
            current = await _current_review(session, evidence.id)
            if (current.id if current else None) != supersedes:
                raise _conflict(
                    "location.review_stale",
                    "Someone else reviewed this in the meantime. Reload to see their decision.",
                )
            review = LocationReview(
                evidence_id=evidence.id,
                employee_id=evidence.employee_id,
                decision=decision,
                reason_code=reason_code,
                note=note,
                supersedes=supersedes,
                reviewed_by=actor.user_id,
                reviewed_at=self._clock.now(),
            )
            session.add(review)
            try:
                await session.flush()
            except UniqueViolationError as exc:
                raise _conflict(
                    "location.review_stale",
                    "Someone else reviewed this in the meantime. Reload to see their decision.",
                ) from exc
            await self._audit.record(
                session,
                AuditEvent(
                    action="attendance.location.reviewed",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="attendance_location_evidence",
                    target_id=evidence.id,
                    subject_employee_id=evidence.employee_id,
                    changes={
                        "decision": ValueChange(current.decision if current else None, decision),
                        # Not "reason_code": the audit writer treats any `code` field as a secret.
                        "review_reason": ValueChange(current.reason_code if current else None, reason_code),
                        "outcome": ValueChange(None, evidence.outcome),
                        "review_id": ValueChange(None, review.id),
                    },
                    reason=note,
                ),
            )
            return review

    @staticmethod
    async def _items(session: Session, rows: Sequence[LocationEvidence]) -> list[QueueItem]:
        refs = await people.employee_refs(session, {row.employee_id for row in rows})
        names = await org.unit_names(session, location_ids={row.location_id for row in rows})
        events = {
            event.id: event
            for event in await session.find(AttendanceEvent, {"id": {"$in": [row.event_id for row in rows]}})
        }
        voided = await voided_ids(session, list(events))
        current: dict[uuid.UUID, LocationReview] = {}
        if rows:
            for review in await session.find(
                LocationReview, {"evidence_id": {"$in": [row.id for row in rows]}}, sort=[("reviewed_at", 1)]
            ):
                current[review.evidence_id] = review
        return [
            QueueItem(
                evidence=row,
                employee_name=refs.get(row.employee_id, ("Unknown person", ""))[0],
                employee_code=refs.get(row.employee_id, ("Unknown person", ""))[1],
                location_name=names.locations.get(row.location_id, "Unknown location"),
                event_occurred_at=events[row.event_id].occurred_at if row.event_id in events else None,
                event_voided=row.event_id in voided,
                current=current.get(row.id),
            )
            for row in rows
        ]


async def _current_review(session: Session, evidence_id: uuid.UUID) -> LocationReview | None:
    reviews = await session.find(
        LocationReview, {"evidence_id": evidence_id}, sort=[("reviewed_at", -1), ("id", -1)], limit=1
    )
    return reviews[0] if reviews else None
