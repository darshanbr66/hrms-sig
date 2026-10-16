"""Leave management (docs/product-requirements.md §6.5, authorization-model.md §3.4).

- Leave types and effective-dated policies are Sigvitas configuration (`leave.policy.manage`).
  Nothing is seeded and no value is assumed. An employee may request a type only while a
  policy covers them (the most specific match for their location and employment type on the
  first day of the request).
- The balance is the sum of the ledger (LV-2). Pending requests reserve their days; the
  available balance is the balance minus the reserved days. Approval writes one `usage`
  entry; cancelling approved leave writes one `usage_reversal`. HR records adjustments and
  opening balances with a reason (`leave.balance.adjust`, never on their own balance,
  SOD-6), and runs the upfront allocation of a leave year, which writes at most one entry per
  employee, leave type and leave year.
- Every change to one employee's leave runs under a per-employee lock, so two requests or
  decisions cannot both pass the balance check, and the database refuses overlapping active
  requests (LV-4). Submissions and decisions carry an `Idempotency-Key`: a retry returns the
  first result instead of acting twice.
- Decisions need `leave.request.approve` covering the employee (team or all), never on one's
  own request (SOD-1), and a note when rejecting. A decision on a request someone else has
  just decided is refused (409).
- Reading follows `leave.read` (self, team, all). Lists and the calendar never include the
  reason; the request detail shows it to the employee and to whoever may decide it, and such a
  read by someone else is audited.
"""

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, and_, func, literal, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance import public as attendance
from app.modules.identity import public as identity
from app.modules.leave import notifications, reports
from app.modules.leave.models import (
    ACTIVE_STATUSES,
    YEARLY_WHERE,
    AccrualMethod,
    CarryForwardMode,
    Decision,
    EntryType,
    Holiday,
    HolidayCalendar,
    HolidayKind,
    LeaveDecision,
    LeaveLedgerEntry,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestDay,
    LeaveType,
    LeaveTypeStatus,
    LocationHolidayCalendar,
    RequestStatus,
)
from app.modules.leave.rules import (
    CountedDay,
    Period,
    RuleError,
    allows_balance,
    count_days,
    leave_year_start,
    most_specific,
    next_leave_year_start,
    prorated,
    total,
    valid_year_start,
)
from app.modules.org import public as org
from app.modules.people import public as people
from app.platform.audit.records import (
    AuditEvent,
    FieldChange,
    RedactedChange,
    RequestContext,
    ValueChange,
    change_of,
)
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer
from app.platform.authz.sod import SodRule
from app.platform.clock import Clock
from app.platform.concurrency import check_version
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.ratelimit import RateLimiter, RateLimitRule, RateLimitScope

# docs/api-architecture.md §9.
SUBMISSIONS_PER_USER: Final = RateLimitRule(RateLimitScope.API, "leave.requests", 10, timedelta(minutes=1))
MAX_CALENDAR_DAYS: Final = 93
ZERO: Final = Decimal("0.00")
MAX_CALENDAR_ENTRIES: Final = 500
MAX_REPORT_DAYS: Final = 366
TYPE_FIELDS: Final = (
    "name",
    "description",
    "is_paid",
    "requires_approval",
    "allows_half_day",
    "counts_weekly_offs",
    "counts_holidays",
    "status",
)


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def check_policy_rules(values: Mapping[str, Any]) -> None:
    """Every rule a new policy needs, tied to its accrual method and year-end choice. Nothing
    is filled in: a missing decision is refused, except the year end, which may stay undecided
    (the year-end run then skips the policy and lists it)."""
    method = values["accrual_method"]
    periodic = method == AccrualMethod.PERIODIC.value
    accrues = method != AccrualMethod.NONE.value
    if accrues != (values.get("accrual_amount") is not None):
        raise field_error(
            "accrual_amount",
            "leave.accrual_amount",
            "Enter the days given each leave year or period, or choose no accrual.",
        )
    if periodic != (values.get("accrual_frequency") is not None and values.get("accrual_timing") is not None):
        raise field_error(
            "accrual_frequency",
            "leave.accrual_period",
            "Periodic accrual needs how often days are given and when in the period.",
        )
    if accrues and values.get("prorate_on_joining") is None:
        raise field_error(
            "prorate_on_joining",
            "leave.proration_undecided",
            "Choose whether people joining part way get a share.",
        )
    if not accrues and values.get("prorate_on_joining") is not None:
        raise field_error(
            "prorate_on_joining", "leave.proration_unused", "Proration applies only with accrual."
        )
    if periodic == (values.get("prorate_on_exit") is None):
        raise field_error(
            "prorate_on_exit",
            "leave.proration_on_exit",
            "Choose whether people leaving part way through a period get a share (periodic accrual only).",
        )
    prorates = bool(values.get("prorate_on_joining")) or bool(values.get("prorate_on_exit"))
    if prorates != (values.get("proration_rounding") is not None):
        raise field_error(
            "proration_rounding",
            "leave.proration_rounding",
            "Choose how a prorated share is rounded, or turn proration off.",
        )
    if values["allow_negative_balance"] != (values.get("max_negative_days") is not None):
        raise field_error(
            "max_negative_days",
            "leave.max_negative_days",
            "Enter how far below zero the balance may go, or do not allow a negative balance.",
        )
    mode = values.get("carry_forward_mode")
    cap, excess, expiry = (
        values.get("carry_forward_cap"),
        values.get("excess_action"),
        values.get("carried_expire_after_months"),
    )
    if (mode == CarryForwardMode.CAPPED.value) != (cap is not None):
        raise field_error(
            "carry_forward_cap",
            "leave.carry_forward_cap",
            "Enter the most days carried, only with a capped carry-forward.",
        )
    if (mode in (CarryForwardMode.NONE.value, CarryForwardMode.CAPPED.value)) != (excess is not None):
        raise field_error(
            "excess_action",
            "leave.excess_action",
            "Choose whether days not carried lapse or stay, only when not everything is carried.",
        )
    if expiry is not None and mode not in (CarryForwardMode.CAPPED.value, CarryForwardMode.UNLIMITED.value):
        raise field_error(
            "carried_expire_after_months",
            "leave.carried_expiry_unused",
            "Carried days can expire only when days are carried forward.",
        )


def _rule(error: RuleError) -> ProblemError:
    return field_error(error.field, error.code, error.message)


@dataclass(frozen=True, slots=True)
class Balance:
    leave_type: LeaveType
    balance: Decimal
    reserved: Decimal
    policy: LeavePolicy | None

    @property
    def available(self) -> Decimal:
        return self.balance - self.reserved


@dataclass(frozen=True, slots=True)
class Draft:
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    start_half: str | None
    end_half: str | None


@dataclass(frozen=True, slots=True)
class Preview:
    days: list[CountedDay]
    days_requested: Decimal
    available_before: Decimal
    within_balance: bool
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class RequestRecord:
    request: LeaveRequest
    employee_name: str
    leave_type_name: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision: LeaveDecision
    decided_by: str


@dataclass(frozen=True, slots=True)
class RequestDetail:
    record: RequestRecord
    days: list[LeaveRequestDay]
    decisions: list[DecisionRecord]
    reason_visible: bool
    can_decide: bool
    can_cancel: bool


@dataclass(frozen=True, slots=True)
class RequestFilters:
    statuses: tuple[str, ...] = ()
    leave_type_id: uuid.UUID | None = None
    employee_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    start: date | None = None
    end: date | None = None


@dataclass(frozen=True, slots=True)
class RequestListing:
    records: list[RequestRecord]
    next_after: tuple[str, uuid.UUID] | None


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    policy: LeavePolicy
    leave_type_name: str
    location_name: str | None


@dataclass(frozen=True, slots=True)
class AllocationCounts:
    allocated: int = 0
    already_allocated: int = 0
    no_policy: int = 0
    no_allocation_in_policy: int = 0
    skipped_own: int = 0
    nothing_due: int = 0


async def _refuse_archive_with_open_requests(session: AsyncSession, leave_type_id: uuid.UUID) -> None:
    """A leave type is archived only once no request of it waits for a decision, so approvers
    are never left deciding requests of a type nobody can request any more."""
    waiting = (
        await session.execute(
            select(func.count()).where(
                LeaveRequest.leave_type_id == leave_type_id,
                LeaveRequest.status.in_(
                    (RequestStatus.PENDING.value, RequestStatus.CANCELLATION_REQUESTED.value)
                ),
            )
        )
    ).scalar_one()
    if waiting:
        raise conflict(
            "leave.type_in_use",
            f"{waiting} request{'s' if waiting != 1 else ''} of this leave type "
            f"{'wait' if waiting != 1 else 'waits'} for a decision. Decide them first, then archive it.",
        )


async def _lock_employee(session: AsyncSession, employee_id: uuid.UUID) -> None:
    """One writer of an employee's leave at a time, so balance checks and writes agree."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"leave:{employee_id}"}
    )


async def _ledger_sums(
    session: AsyncSession, employee_id: uuid.UUID, type_ids: Sequence[uuid.UUID] | None = None
) -> dict[uuid.UUID, Decimal]:
    query = (
        select(LeaveLedgerEntry.leave_type_id, func.sum(LeaveLedgerEntry.amount))
        .where(LeaveLedgerEntry.employee_id == employee_id)
        .group_by(LeaveLedgerEntry.leave_type_id)
    )
    if type_ids is not None:
        query = query.where(LeaveLedgerEntry.leave_type_id.in_(type_ids))
    return {row[0]: Decimal(row[1]) for row in (await session.execute(query)).all()}


async def _reserved(
    session: AsyncSession, employee_id: uuid.UUID, *, excluding: uuid.UUID | None = None
) -> dict[uuid.UUID, Decimal]:
    query = (
        select(LeaveRequest.leave_type_id, func.sum(LeaveRequest.days_requested))
        .where(LeaveRequest.employee_id == employee_id, LeaveRequest.status == RequestStatus.PENDING.value)
        .group_by(LeaveRequest.leave_type_id)
    )
    if excluding is not None:
        query = query.where(LeaveRequest.id != excluding)
    return {row[0]: Decimal(row[1]) for row in (await session.execute(query)).all()}


async def _policy_for(
    session: AsyncSession, leave_type_id: uuid.UUID, facts: people.JobFacts | None, on: date
) -> LeavePolicy | None:
    if facts is None:
        return None
    candidates = list(
        await session.scalars(
            select(LeavePolicy).where(
                LeavePolicy.leave_type_id == leave_type_id,
                LeavePolicy.effective_from <= on,
                or_(LeavePolicy.effective_to.is_(None), LeavePolicy.effective_to > on),
            )
        )
    )
    return most_specific(candidates, facts.location_id, facts.employment_type)


async def _holidays(
    session: AsyncSession, location_id: uuid.UUID | None, start: date, end: date
) -> set[date]:
    """Holidays (not optional ones) of the active calendar each year of the range assigns to
    the location."""
    if location_id is None:
        return set()
    rows = await session.scalars(
        select(Holiday.date)
        .join(HolidayCalendar, HolidayCalendar.id == Holiday.calendar_id)
        .join(LocationHolidayCalendar, LocationHolidayCalendar.calendar_id == HolidayCalendar.id)
        .where(
            LocationHolidayCalendar.location_id == location_id,
            HolidayCalendar.status == "active",
            Holiday.kind == HolidayKind.HOLIDAY.value,
            Holiday.is_optional.is_(False),
            Holiday.date >= start,
            Holiday.date <= end,
        )
    )
    return set(rows)


def _own_employee(actor: Actor) -> uuid.UUID:
    if actor.employee_id is None:
        raise ProblemError(ProblemType.NOT_FOUND, detail="Your account is not linked to an employee record.")
    return actor.employee_id


def _audit_name(field: str) -> str:
    return {"accrual_amount": "accrual_days", "carry_forward_cap": "carry_forward_cap_days"}.get(field, field)


def _changes(values: Mapping[str, Any], before: Any = None) -> dict[str, FieldChange]:
    return {
        name: change_of(name, getattr(before, name) if before is not None else None, value)
        for name, value in values.items()
        if before is None or getattr(before, name) != value
    }


class LeaveService:
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

    # --- leave types ---------------------------------------------------------------------------

    async def leave_types(self, actor: Actor, *, include_archived: bool) -> list[LeaveType]:
        query = select(LeaveType).order_by(func.lower(LeaveType.name), LeaveType.id)
        if not include_archived:
            query = query.where(LeaveType.status == LeaveTypeStatus.ACTIVE.value)
        async with self._db.unit_of_work() as session:
            return list(await session.scalars(query))

    async def create_type(
        self, actor: Actor, context: RequestContext, values: Mapping[str, Any]
    ) -> LeaveType:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "leave.policy.manage")
            leave_type = LeaveType(**values)
            session.add(leave_type)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError as exc:
                raise conflict(
                    "leave.type_code_in_use", "Another leave type already uses this code."
                ) from exc
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.type.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_type",
                    target_id=leave_type.id,
                    changes=_changes(values),
                ),
            )
            await session.refresh(leave_type)
            return leave_type

    async def update_type(
        self,
        actor: Actor,
        context: RequestContext,
        leave_type_id: uuid.UUID,
        *,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> LeaveType:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "leave.policy.manage")
            leave_type = await session.get(
                LeaveType, leave_type_id, with_for_update=True, populate_existing=True
            )
            if leave_type is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            check_version(leave_type.version, expected_version)
            for name, value in values.items():
                if value is None and name != "description":
                    raise field_error(name, "leave.required", "Choose a value.")
            if (
                values.get("status") == LeaveTypeStatus.ARCHIVED
                and leave_type.status != LeaveTypeStatus.ARCHIVED
            ):
                await _refuse_archive_with_open_requests(session, leave_type.id)
            changes = _changes(values, leave_type)
            if changes:
                for name, value in values.items():
                    setattr(leave_type, name, value)
                leave_type.version += 1
                leave_type.updated_at = self._clock.now()
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="leave.type.updated",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used=granted.key,
                        target_type="leave_type",
                        target_id=leave_type.id,
                        changes=changes,
                    ),
                )
            await session.flush()
            await session.refresh(leave_type)
            return leave_type

    # --- policies ------------------------------------------------------------------------------

    async def policies(self, leave_type_id: uuid.UUID | None) -> list[PolicyRecord]:
        query = select(LeavePolicy, LeaveType.name).join(LeaveType, LeaveType.id == LeavePolicy.leave_type_id)
        if leave_type_id is not None:
            query = query.where(LeavePolicy.leave_type_id == leave_type_id)
        query = query.order_by(
            func.lower(LeaveType.name),
            LeavePolicy.location_id.is_not(None),
            LeavePolicy.employment_type.is_not(None),
            LeavePolicy.effective_from.desc(),
        )
        async with self._db.unit_of_work() as session:
            rows = (await session.execute(query)).all()
            locations = await org.unit_names(
                session, location_ids={r[0].location_id for r in rows if r[0].location_id}
            )
        return [
            PolicyRecord(
                row[0], row[1], locations.locations.get(row[0].location_id) if row[0].location_id else None
            )
            for row in rows
        ]

    async def create_policy(
        self, actor: Actor, context: RequestContext, values: Mapping[str, Any]
    ) -> PolicyRecord:
        if not valid_year_start(values["leave_year_start_month"], values["leave_year_start_day"]):
            raise field_error(
                "leave_year_start_day",
                "leave.year_start_invalid",
                "Choose a day that exists in every year (29 February does not).",
            )
        check_policy_rules(values)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "leave.policy.manage")
            leave_type = await session.get(LeaveType, values["leave_type_id"])
            if leave_type is None or leave_type.status != LeaveTypeStatus.ACTIVE.value:
                raise field_error("leave_type_id", "leave.unknown_type", "Choose an active leave type.")
            location_id = values.get("location_id")
            if location_id is not None and not await org.location_time_zones(session, [location_id]):
                raise field_error("location_id", "leave.unknown_location", "Choose an existing location.")
            # One writer per leave type, so two versions of one scope cannot race.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"leave_policy:{leave_type.id}"},
            )
            same_scope = and_(
                LeavePolicy.leave_type_id == leave_type.id,
                LeavePolicy.location_id.is_(None)
                if location_id is None
                else LeavePolicy.location_id == location_id,
                LeavePolicy.employment_type.is_(None)
                if values.get("employment_type") is None
                else LeavePolicy.employment_type == values["employment_type"],
            )
            later = (
                await session.scalars(
                    select(LeavePolicy).where(
                        same_scope,
                        or_(
                            LeavePolicy.effective_to.is_(None),
                            LeavePolicy.effective_from >= values["effective_from"],
                            LeavePolicy.effective_to > values["effective_from"],
                        ),
                    )
                )
            ).all()
            current = [
                p for p in later if p.effective_from < values["effective_from"] and p.effective_to is None
            ]
            if len(later) != len(current):
                raise conflict(
                    "leave.policy_overlap",
                    "A version of this policy already starts on or after that date, or ends after it. "
                    "Choose a later start date.",
                )
            for previous in current:
                previous.effective_to = values["effective_from"]
                previous.updated_at = self._clock.now()
            await session.flush()
            policy = LeavePolicy(**values, created_by=actor.user_id)
            session.add(policy)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.policy.created",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_policy",
                    target_id=policy.id,
                    # Day counts, not money: named so the audit backstop does not withhold them.
                    changes={
                        **_changes({_audit_name(k): v for k, v in values.items()}),
                        "replaces": ValueChange(None, current[0].id if current else None),
                    },
                ),
            )
            await session.refresh(policy)
            names = await org.unit_names(session, location_ids={location_id} if location_id else set())
            return PolicyRecord(
                policy, leave_type.name, names.locations.get(location_id) if location_id else None
            )

    # --- balances and the ledger ---------------------------------------------------------------

    async def balances(self, actor: Actor, employee_id: uuid.UUID | None) -> list[Balance]:
        subject = employee_id or _own_employee(actor)
        today = self._authz.today()
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "leave.read", subject_employee_id=subject, hide_existence=True
            )
            sums = await _ledger_sums(session, subject)
            reserved = await _reserved(session, subject)
            types = list(
                await session.scalars(
                    select(LeaveType)
                    .where(
                        or_(
                            LeaveType.status == LeaveTypeStatus.ACTIVE.value,
                            LeaveType.id.in_(list(sums) + list(reserved)),
                        )
                    )
                    .order_by(func.lower(LeaveType.name), LeaveType.id)
                )
            )
            facts = await people.job_facts(session, subject, today)
            balances = []
            for leave_type in types:
                policy = await _policy_for(session, leave_type.id, facts, today)
                if policy is None and leave_type.id not in sums and leave_type.id not in reserved:
                    continue
                balances.append(
                    Balance(
                        leave_type,
                        sums.get(leave_type.id, ZERO),
                        reserved.get(leave_type.id, ZERO),
                        policy if leave_type.status == LeaveTypeStatus.ACTIVE.value else None,
                    )
                )
            return balances

    async def ledger(
        self, actor: Actor, employee_id: uuid.UUID | None, leave_type_id: uuid.UUID | None
    ) -> list[tuple[LeaveLedgerEntry, str]]:
        subject = employee_id or _own_employee(actor)
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "leave.read", subject_employee_id=subject, hide_existence=True
            )
            query = (
                select(LeaveLedgerEntry, LeaveType.name)
                .join(LeaveType, LeaveType.id == LeaveLedgerEntry.leave_type_id)
                .where(LeaveLedgerEntry.employee_id == subject)
                .order_by(LeaveLedgerEntry.effective_date.desc(), LeaveLedgerEntry.id.desc())
                .limit(500)
            )
            if leave_type_id is not None:
                query = query.where(LeaveLedgerEntry.leave_type_id == leave_type_id)
            return [(row[0], row[1]) for row in (await session.execute(query)).all()]

    async def adjust(
        self,
        actor: Actor,
        context: RequestContext,
        employee_id: uuid.UUID,
        values: Mapping[str, Any],
        idempotency_key: str,
    ) -> LeaveLedgerEntry:
        if values["amount"] == 0:
            raise field_error("amount", "leave.amount_zero", "Enter a number of days other than zero.")
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, "leave.balance.adjust", subject_employee_id=employee_id, hide_existence=True
            )
            if actor.employee_id == employee_id:
                raise AccessDenied(code=SodRule.OWN_BALANCE.value)
            await _lock_employee(session, employee_id)
            replay = (
                await session.scalars(
                    select(LeaveLedgerEntry).where(
                        LeaveLedgerEntry.employee_id == employee_id,
                        LeaveLedgerEntry.idempotency_key == idempotency_key,
                    )
                )
            ).one_or_none()
            if replay is not None:
                if (replay.leave_type_id, replay.amount, replay.entry_type) != (
                    values["leave_type_id"],
                    values["amount"],
                    values["entry_type"],
                ):
                    raise field_error(
                        "Idempotency-Key",
                        "idempotency.key_reused",
                        "This request key was already used for a different change. Reload and try again.",
                    )
                return replay
            leave_type = await session.get(LeaveType, values["leave_type_id"])
            if leave_type is None:
                raise field_error("leave_type_id", "leave.unknown_type", "Choose an existing leave type.")
            facts = await people.job_facts(session, employee_id, values["effective_date"])
            policy = await _policy_for(session, leave_type.id, facts, values["effective_date"])
            if policy is None:
                raise field_error(
                    "leave_type_id",
                    "leave.not_eligible",
                    "No leave policy covers this employee for this leave type on that date. Add one first.",
                )
            entry = LeaveLedgerEntry(
                employee_id=employee_id,
                leave_type_id=leave_type.id,
                leave_year=leave_year_start(
                    values["effective_date"], policy.leave_year_start_month, policy.leave_year_start_day
                ),
                entry_type=values["entry_type"],
                amount=values["amount"],
                effective_date=values["effective_date"],
                policy_id=policy.id,
                reason=values["reason"],
                created_by=actor.user_id,
                idempotency_key=idempotency_key,
            )
            session.add(entry)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError as exc:
                raise conflict(
                    "leave.opening_balance_exists",
                    "An opening balance for this leave year is already recorded. "
                    "Record an adjustment instead.",
                ) from exc
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.balance.adjusted",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_ledger",
                    target_id=entry.id,
                    subject_employee_id=employee_id,
                    changes={
                        "leave_type_id": ValueChange(None, leave_type.id),
                        "entry_type": ValueChange(None, entry.entry_type),
                        "days": ValueChange(None, entry.amount),
                    },
                    reason=values["reason"],
                ),
            )
            await session.refresh(entry)
            return entry

    async def allocate(
        self, actor: Actor, context: RequestContext, leave_type_id: uuid.UUID, as_of: date
    ) -> AllocationCounts:
        """The upfront allocation of the leave year `as_of` falls in, for every employee
        employed on that date whom the actor may adjust. Each employee's own policy on that
        date decides the amount; an employee already allocated for that year is left alone."""
        counts: dict[str, int] = defaultdict(int)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "leave.balance.adjust")
            leave_type = await session.get(LeaveType, leave_type_id)
            if leave_type is None or leave_type.status != LeaveTypeStatus.ACTIVE.value:
                raise field_error("leave_type_id", "leave.unknown_type", "Choose an active leave type.")
            colleagues, _ = await people.employed_colleagues(
                session, people.employee_id_column().is_not(None), on=as_of
            )
            for colleague in colleagues:
                try:
                    await self._authz.require(
                        session, actor, "leave.balance.adjust", subject_employee_id=colleague.id
                    )
                except AccessDenied:
                    continue
                if colleague.id == actor.employee_id:
                    counts["skipped_own"] += 1
                    continue
                facts = await people.job_facts(session, colleague.id, as_of)
                policy = await _policy_for(session, leave_type.id, facts, as_of)
                if policy is None:
                    counts["no_policy"] += 1
                    continue
                if (
                    policy.accrual_method != AccrualMethod.UPFRONT_PER_LEAVE_YEAR.value
                    or policy.accrual_amount is None
                ):
                    counts["no_allocation_in_policy"] += 1
                    continue
                year = leave_year_start(as_of, policy.leave_year_start_month, policy.leave_year_start_day)
                amount = policy.accrual_amount
                if policy.prorate_on_joining and facts is not None:
                    amount = prorated(
                        amount,
                        Period(year, next_leave_year_start(year) - timedelta(days=1)),
                        facts.date_of_joining,
                        None,
                        on_joining=True,
                        on_exit=False,
                        rounding=policy.proration_rounding,
                    )
                if amount <= 0:
                    counts["nothing_due"] += 1
                    continue
                inserted = (
                    await session.execute(
                        insert(LeaveLedgerEntry)
                        .values(
                            employee_id=colleague.id,
                            leave_type_id=leave_type.id,
                            leave_year=year,
                            entry_type=EntryType.ACCRUAL.value,
                            amount=amount,
                            effective_date=max(year, facts.date_of_joining) if facts else year,
                            policy_id=policy.id,
                            created_by=actor.user_id,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                LeaveLedgerEntry.employee_id,
                                LeaveLedgerEntry.leave_type_id,
                                LeaveLedgerEntry.leave_year,
                                LeaveLedgerEntry.entry_type,
                            ],
                            index_where=text(YEARLY_WHERE),
                        )
                        .returning(LeaveLedgerEntry.id)
                    )
                ).scalar_one_or_none()
                counts["allocated" if inserted else "already_allocated"] += 1
            result = AllocationCounts(**counts)
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.allocation.run",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_type",
                    target_id=leave_type.id,
                    changes={name: ValueChange(None, value) for name, value in vars_of(result).items()}
                    | {"as_of": ValueChange(None, as_of)},
                ),
            )
            return result

    # --- requests: preview and submit ----------------------------------------------------------

    async def _evaluate(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
        draft: Draft,
        *,
        excluding: uuid.UUID | None = None,
    ) -> tuple[LeaveType, LeavePolicy, Preview]:
        leave_type = await session.get(LeaveType, draft.leave_type_id)
        if leave_type is None or leave_type.status != LeaveTypeStatus.ACTIVE.value:
            raise field_error("leave_type_id", "leave.unknown_type", "Choose an active leave type.")
        facts = await people.job_facts(session, employee_id, draft.start_date)
        if facts is None or not facts.employed_on(draft.start_date) or not facts.employed_on(draft.end_date):
            raise field_error(
                "start_date",
                "leave.not_employed",
                "Choose dates between your joining date and your last working day.",
            )
        policy = await _policy_for(session, leave_type.id, facts, draft.start_date)
        if policy is None:
            raise field_error(
                "leave_type_id",
                "leave.not_eligible",
                "No leave policy covers you for this leave type on these dates. Ask HR.",
            )
        try:
            days = count_days(
                draft.start_date,
                draft.end_date,
                draft.start_half,
                draft.end_half,
                allows_half_day=leave_type.allows_half_day,
                holidays=await _holidays(session, facts.location_id, draft.start_date, draft.end_date),
                counts_holidays=leave_type.counts_holidays,
                # Weekly offs of the shift that applies on each date, less declared working
                # days; none without a shift.
                weekly_offs=await attendance.weekly_off_dates(
                    session, employee_id, draft.start_date, draft.end_date
                ),
                counts_weekly_offs=leave_type.counts_weekly_offs,
            )
        except RuleError as error:
            raise _rule(error) from error
        requested = total(days)
        if requested <= 0:
            raise field_error(
                "end_date",
                "leave.no_days",
                "Every day in this range is a holiday, so there is nothing to request.",
            )
        sums = await _ledger_sums(session, employee_id, [leave_type.id])
        reserved = await _reserved(session, employee_id, excluding=excluding)
        available = sums.get(leave_type.id, ZERO) - reserved.get(leave_type.id, ZERO)
        within = allows_balance(
            available,
            requested,
            allow_negative=policy.allow_negative_balance,
            max_negative=policy.max_negative_days,
        )
        return leave_type, policy, Preview(days, requested, available, within, leave_type.requires_approval)

    async def preview(self, actor: Actor, draft: Draft) -> Preview:
        employee_id = _own_employee(actor)
        async with self._db.unit_of_work() as session:
            await self._authz.require(session, actor, "leave.request.self", subject_employee_id=employee_id)
            _, _, preview = await self._evaluate(session, employee_id, draft)
            return preview

    async def submit(
        self, actor: Actor, context: RequestContext, draft: Draft, reason: str | None, idempotency_key: str
    ) -> RequestDetail:
        employee_id = _own_employee(actor)
        await self._limiter.enforce(SUBMISSIONS_PER_USER, str(actor.user_id))
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, "leave.request.self", subject_employee_id=employee_id
            )
            await _lock_employee(session, employee_id)
            replay = (
                await session.scalars(
                    select(LeaveRequest).where(
                        LeaveRequest.employee_id == employee_id,
                        LeaveRequest.idempotency_key == idempotency_key,
                    )
                )
            ).one_or_none()
            if replay is not None:
                if (replay.leave_type_id, replay.start_date, replay.end_date) != (
                    draft.leave_type_id,
                    draft.start_date,
                    draft.end_date,
                ):
                    raise field_error(
                        "Idempotency-Key",
                        "idempotency.key_reused",
                        "This request key was already used for a different request. Reload and try again.",
                    )
                return await self._detail(session, actor, replay.id)
            leave_type, policy, preview = await self._evaluate(session, employee_id, draft)
            if not preview.within_balance:
                raise conflict(
                    "leave.insufficient_balance",
                    f"You have {preview.available_before} days available for {leave_type.name}; this request "
                    f"needs {preview.days_requested}.",
                )
            auto = not leave_type.requires_approval
            request = LeaveRequest(
                employee_id=employee_id,
                leave_type_id=leave_type.id,
                start_date=draft.start_date,
                end_date=draft.end_date,
                start_half=draft.start_half,
                end_half=draft.end_half,
                days_requested=preview.days_requested,
                reason=reason,
                status=(RequestStatus.APPROVED if auto else RequestStatus.PENDING).value,
                submitted_by=actor.user_id,
                idempotency_key=idempotency_key,
            )
            session.add(request)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError as exc:
                raise conflict(
                    "leave.overlap", "You already have leave requested or approved on some of these days."
                ) from exc
            session.add_all(
                LeaveRequestDay(
                    leave_request_id=request.id,
                    date=day.date,
                    portion=day.portion,
                    counted=day.counted,
                    not_counted_reason=day.not_counted_reason,
                )
                for day in preview.days
            )
            if auto:
                self._usage(session, request, policy, actor.user_id)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.request.submitted",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_request",
                    target_id=request.id,
                    subject_employee_id=employee_id,
                    changes={
                        "leave_type_id": ValueChange(None, leave_type.id),
                        "start_date": ValueChange(None, request.start_date),
                        "end_date": ValueChange(None, request.end_date),
                        "days_requested": ValueChange(None, request.days_requested),
                        "status": ValueChange(None, request.status),
                        **({"reason": RedactedChange()} if reason else {}),
                    },
                ),
            )
            now = self._clock.now()
            if auto:
                await notifications.recorded(session, request, leave_type.name, now)
            else:
                await notifications.to_manager(
                    session, request, leave_type.name, "submitted", now=now, today=self._authz.today()
                )
            return await self._detail(session, actor, request.id)

    @staticmethod
    def _usage(session: AsyncSession, request: LeaveRequest, policy: LeavePolicy, user_id: uuid.UUID) -> None:
        session.add(
            LeaveLedgerEntry(
                employee_id=request.employee_id,
                leave_type_id=request.leave_type_id,
                leave_year=leave_year_start(
                    request.start_date, policy.leave_year_start_month, policy.leave_year_start_day
                ),
                entry_type=EntryType.USAGE.value,
                amount=-request.days_requested,
                effective_date=request.start_date,
                leave_request_id=request.id,
                policy_id=policy.id,
                created_by=user_id,
            )
        )

    async def _reversal(self, session: AsyncSession, request: LeaveRequest, user_id: uuid.UUID) -> None:
        usage = (
            await session.scalars(
                select(LeaveLedgerEntry).where(
                    LeaveLedgerEntry.leave_request_id == request.id,
                    LeaveLedgerEntry.entry_type == EntryType.USAGE.value,
                )
            )
        ).one_or_none()
        if usage is None:
            return
        session.add(
            LeaveLedgerEntry(
                employee_id=usage.employee_id,
                leave_type_id=usage.leave_type_id,
                leave_year=usage.leave_year,
                entry_type=EntryType.USAGE_REVERSAL.value,
                amount=-usage.amount,
                effective_date=usage.effective_date,
                leave_request_id=request.id,
                policy_id=usage.policy_id,
                created_by=user_id,
            )
        )

    # --- requests: cancel and decide -----------------------------------------------------------

    async def cancel(self, actor: Actor, context: RequestContext, request_id: uuid.UUID) -> RequestDetail:
        """The employee's own cancellation (LV-6): a pending request is withdrawn; approved leave
        that has not started is cancelled at once when its type needs no approval, otherwise
        its cancellation goes to the approver."""
        employee_id = _own_employee(actor)
        today = self._authz.today()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(
                session, actor, "leave.request.self", subject_employee_id=employee_id
            )
            await _lock_employee(session, employee_id)
            request = await session.get(
                LeaveRequest, request_id, with_for_update=True, populate_existing=True
            )
            if request is None or request.employee_id != employee_id:
                raise ProblemError(ProblemType.NOT_FOUND)
            leave_type = await session.get(LeaveType, request.leave_type_id)
            if leave_type is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            before = request.status
            if request.status == RequestStatus.PENDING:
                request.status = RequestStatus.CANCELLED.value
            elif request.status == RequestStatus.APPROVED:
                if request.start_date <= today:
                    raise conflict(
                        "leave.already_started",
                        "This leave has started, so it can no longer be cancelled here. Ask HR.",
                    )
                if leave_type.requires_approval:
                    request.status = RequestStatus.CANCELLATION_REQUESTED.value
                else:
                    request.status = RequestStatus.CANCELLED.value
                    await self._reversal(session, request, actor.user_id)
            else:
                raise conflict(
                    "leave.not_cancellable", "This request cannot be cancelled in its current state."
                )
            request.version += 1
            request.updated_at = self._clock.now()
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.request.cancelled"
                    if request.status == RequestStatus.CANCELLED
                    else "leave.request.cancellation_requested",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_request",
                    target_id=request.id,
                    subject_employee_id=employee_id,
                    changes={"status": ValueChange(before, request.status)},
                ),
            )
            if request.status == RequestStatus.CANCELLATION_REQUESTED:
                await notifications.to_manager(
                    session,
                    request,
                    leave_type.name,
                    "cancellation_requested",
                    now=self._clock.now(),
                    today=today,
                )
            return await self._detail(session, actor, request.id)

    async def decide(
        self,
        actor: Actor,
        context: RequestContext,
        request_id: uuid.UUID,
        *,
        approve: bool,
        note: str | None,
        idempotency_key: str,
    ) -> RequestDetail:
        if not approve and note is None:
            raise field_error(
                "note", "leave.note_required", "Say why, so the employee knows what to do next."
            )
        async with self._db.unit_of_work() as session:
            subject = (
                await session.execute(select(LeaveRequest.employee_id).where(LeaveRequest.id == request_id))
            ).scalar_one_or_none()
            if subject is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            granted = await self._authz.require(
                session, actor, "leave.request.approve", subject_employee_id=subject, hide_existence=True
            )
            if actor.employee_id == subject:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            replay = (
                await session.scalars(
                    select(LeaveDecision).where(
                        LeaveDecision.decided_by == actor.user_id,
                        LeaveDecision.idempotency_key == idempotency_key,
                    )
                )
            ).one_or_none()
            if replay is not None:
                if replay.leave_request_id != request_id:
                    raise field_error(
                        "Idempotency-Key",
                        "idempotency.key_reused",
                        "This request key was already used for a different decision. Reload and try again.",
                    )
                return await self._detail(session, actor, request_id)
            await _lock_employee(session, subject)
            request = await session.get(
                LeaveRequest, request_id, with_for_update=True, populate_existing=True
            )
            if request is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if request.submitted_by == actor.user_id:
                raise AccessDenied(code=SodRule.OWN_REQUEST.value)
            leave_type = await session.get(LeaveType, request.leave_type_id)
            if leave_type is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            before = request.status
            decision = await self._transition(session, request, approve=approve, user_id=actor.user_id)
            request.version += 1
            request.updated_at = self._clock.now()
            session.add(
                LeaveDecision(
                    leave_request_id=request.id,
                    decision=decision.value,
                    decided_by=actor.user_id,
                    note=note,
                    idempotency_key=idempotency_key,
                )
            )
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action=f"leave.request.{decision.value}",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_request",
                    target_id=request.id,
                    subject_employee_id=request.employee_id,
                    changes={
                        "status": ValueChange(before, request.status),
                        **({"note": RedactedChange()} if note else {}),
                    },
                ),
            )
            await notifications.to_employee(
                session, request, leave_type.name, decision.value, self._clock.now()
            )
            return await self._detail(session, actor, request.id)

    async def _transition(
        self, session: AsyncSession, request: LeaveRequest, *, approve: bool, user_id: uuid.UUID
    ) -> Decision:
        """Move a request waiting for a decision to its next state, writing the ledger entry
        the decision causes; refuse a request that is no longer waiting."""
        if request.status == RequestStatus.CANCELLATION_REQUESTED:
            if approve:
                await self._reversal(session, request, user_id)
                request.status = RequestStatus.CANCELLED.value
                return Decision.CANCELLATION_APPROVED
            request.status = RequestStatus.APPROVED.value
            return Decision.CANCELLATION_REJECTED
        if request.status != RequestStatus.PENDING:
            raise conflict(
                "leave.request.already_decided",
                "This request has already been decided or withdrawn. Reload to see its current state.",
            )
        if not approve:
            request.status = RequestStatus.REJECTED.value
            return Decision.REJECTED
        facts = await people.job_facts(session, request.employee_id, request.start_date)
        policy = await _policy_for(session, request.leave_type_id, facts, request.start_date)
        if policy is None:
            raise conflict(
                "leave.not_eligible",
                "No leave policy covers this employee for these dates any more. Ask HR to check the policy.",
            )
        sums = await _ledger_sums(session, request.employee_id, [request.leave_type_id])
        others = await _reserved(session, request.employee_id, excluding=request.id)
        available = sums.get(request.leave_type_id, ZERO) - others.get(request.leave_type_id, ZERO)
        if not allows_balance(
            available,
            request.days_requested,
            allow_negative=policy.allow_negative_balance,
            max_negative=policy.max_negative_days,
        ):
            raise conflict(
                "leave.insufficient_balance",
                f"The employee has {available} days available; this request needs {request.days_requested}.",
            )
        self._usage(session, request, policy, user_id)
        request.status = RequestStatus.APPROVED.value
        return Decision.APPROVED

    # --- reading requests ----------------------------------------------------------------------

    async def detail(self, actor: Actor, context: RequestContext, request_id: uuid.UUID) -> RequestDetail:
        async with self._db.unit_of_work() as session:
            subject = (
                await session.execute(select(LeaveRequest.employee_id).where(LeaveRequest.id == request_id))
            ).scalar_one_or_none()
            if subject is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            await self._authz.require(
                session, actor, "leave.read", subject_employee_id=subject, hide_existence=True
            )
            detail = await self._detail(session, actor, request_id)
            if detail.reason_visible and detail.record.request.reason and actor.employee_id != subject:
                await self._audit.record(
                    session,
                    AuditEvent(
                        action="leave.request.reason_read",
                        actor=actor.audit_actor(),
                        context=context,
                        permission_used="leave.request.approve",
                        target_type="leave_request",
                        target_id=request_id,
                        subject_employee_id=subject,
                    ),
                )
            return detail

    async def _detail(self, session: AsyncSession, actor: Actor, request_id: uuid.UUID) -> RequestDetail:
        request = await session.get(LeaveRequest, request_id, populate_existing=True)
        if request is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        own = actor.employee_id == request.employee_id
        can_decide = False
        if (
            not own
            and request.submitted_by != actor.user_id
            and self._authz.holds_any(actor, "leave.request.approve")
        ):
            try:
                await self._authz.require(
                    session, actor, "leave.request.approve", subject_employee_id=request.employee_id
                )
                can_decide = True
            except AccessDenied:
                can_decide = False
        today = self._authz.today()
        can_cancel = own and (
            request.status == RequestStatus.PENDING
            or (request.status == RequestStatus.APPROVED and request.start_date > today)
        )
        record = (await self._records(session, [request]))[0]
        days = list(
            await session.scalars(
                select(LeaveRequestDay)
                .where(LeaveRequestDay.leave_request_id == request.id)
                .order_by(LeaveRequestDay.date)
            )
        )
        decisions = list(
            await session.scalars(
                select(LeaveDecision)
                .where(LeaveDecision.leave_request_id == request.id)
                .order_by(LeaveDecision.decided_at, LeaveDecision.id)
            )
        )
        deciders = await identity.emails_of(session, {d.decided_by for d in decisions})
        return RequestDetail(
            record=record,
            days=days,
            decisions=[DecisionRecord(d, deciders.get(d.decided_by, "An approver")) for d in decisions],
            reason_visible=own or can_decide,
            can_decide=can_decide
            and request.status in (RequestStatus.PENDING, RequestStatus.CANCELLATION_REQUESTED),
            can_cancel=can_cancel,
        )

    @staticmethod
    async def _records(session: AsyncSession, requests: Sequence[LeaveRequest]) -> list[RequestRecord]:
        names = await people.employee_names(session, {r.employee_id for r in requests})
        types = (
            dict(
                (
                    await session.execute(
                        select(LeaveType.id, LeaveType.name).where(
                            LeaveType.id.in_({r.leave_type_id for r in requests})
                        )
                    )
                ).all()
            )
            if requests
            else {}
        )
        return [
            RequestRecord(r, names.get(r.employee_id, ""), types.get(r.leave_type_id, "")) for r in requests
        ]

    async def requests(
        self,
        actor: Actor,
        filters: RequestFilters,
        *,
        own: bool,
        for_decision: bool,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> RequestListing:
        """Requests in the actor's scope, newest start first; or their own; or, with
        `for_decision`, those waiting for a decision they may make (oldest start first)."""
        today = self._authz.today()
        if own:
            scope: ColumnElement[bool] = LeaveRequest.employee_id == _own_employee(actor)
        elif for_decision:
            scope = and_(
                self._authz.scope_filter(actor, "leave.request.approve", LeaveRequest.employee_id),
                LeaveRequest.status.in_(
                    [RequestStatus.PENDING.value, RequestStatus.CANCELLATION_REQUESTED.value]
                ),
                LeaveRequest.submitted_by != actor.user_id,
                *([LeaveRequest.employee_id != actor.employee_id] if actor.employee_id else []),
            )
        else:
            scope = self._authz.scope_filter(actor, "leave.read", LeaveRequest.employee_id)
        query = select(LeaveRequest).where(scope)
        if filters.statuses:
            query = query.where(LeaveRequest.status.in_(filters.statuses))
        if filters.leave_type_id:
            query = query.where(LeaveRequest.leave_type_id == filters.leave_type_id)
        if filters.employee_id:
            query = query.where(LeaveRequest.employee_id == filters.employee_id)
        if filters.start:
            query = query.where(LeaveRequest.end_date >= filters.start)
        if filters.end:
            query = query.where(LeaveRequest.start_date <= filters.end)
        if filters.department_id or filters.location_id:
            query = query.where(
                LeaveRequest.employee_id.in_(
                    people.placed_employee_ids_query(filters.department_id, filters.location_id, today)
                )
            )
        key = func.to_char(LeaveRequest.start_date, "YYYY-MM-DD")
        ascending = for_decision
        if after is not None:
            position, bound = tuple_(key, LeaveRequest.id), tuple_(literal(after[0]), literal(after[1]))
            query = query.where(position > bound if ascending else position < bound)
        order = (key, LeaveRequest.id) if ascending else (key.desc(), LeaveRequest.id.desc())
        async with self._db.unit_of_work() as session:
            rows = list(await session.scalars(query.order_by(*order).limit(limit + 1)))
            records = await self._records(session, rows[:limit])
        last = rows[limit - 1] if len(rows) > limit else None
        return RequestListing(records, (last.start_date.isoformat(), last.id) if last else None)

    async def calendar(
        self, actor: Actor, start: date, end: date, filters: RequestFilters
    ) -> tuple[list[RequestRecord], bool]:
        if end < start:
            raise field_error("to", "range.reversed", "The end date must be on or after the start date.")
        if (end - start).days >= MAX_CALENDAR_DAYS:
            raise field_error("to", "range.too_long", f"Choose a range of at most {MAX_CALENDAR_DAYS} days.")
        today = self._authz.today()
        query = select(LeaveRequest).where(
            self._authz.scope_filter(actor, "leave.read", LeaveRequest.employee_id),
            LeaveRequest.status.in_([s.value for s in ACTIVE_STATUSES]),
            LeaveRequest.end_date >= start,
            LeaveRequest.start_date <= end,
        )
        if filters.leave_type_id:
            query = query.where(LeaveRequest.leave_type_id == filters.leave_type_id)
        if filters.department_id or filters.location_id:
            query = query.where(
                LeaveRequest.employee_id.in_(
                    people.placed_employee_ids_query(filters.department_id, filters.location_id, today)
                )
            )
        query = query.order_by(LeaveRequest.start_date, LeaveRequest.id).limit(MAX_CALENDAR_ENTRIES + 1)
        async with self._db.unit_of_work() as session:
            rows = list(await session.scalars(query))
            records = await self._records(session, rows[:MAX_CALENDAR_ENTRIES])
        return records, len(rows) > MAX_CALENDAR_ENTRIES

    async def report(
        self,
        actor: Actor,
        *,
        start: date,
        end: date,
        department_id: uuid.UUID | None,
        location_id: uuid.UUID | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> tuple[list[reports.TakenRow], tuple[str, uuid.UUID] | None]:
        """Days taken and pending per person and leave type in a period, for the people in the
        reader's scope employed at some point in it (RPT-1). No reasons."""
        if end < start:
            raise field_error("to", "range.reversed", "The end date must be on or after the start date.")
        if (end - start).days >= MAX_REPORT_DAYS:
            raise field_error("to", "range.too_long", f"Choose a period of at most {MAX_REPORT_DAYS} days.")
        scope = self._authz.scope_filter(actor, "leave.read", people.employee_id_column(), on=end)
        async with self._db.unit_of_work() as session:
            page, next_after = await people.employed_colleagues(
                session,
                scope,
                on=end,
                since=start,
                department_id=department_id,
                location_id=location_id,
                after=after,
                limit=limit,
            )
            return await reports.taken(session, page, start, end), next_after


def vars_of(counts: AllocationCounts) -> dict[str, int]:
    return {
        "allocated": counts.allocated,
        "already_allocated": counts.already_allocated,
        "no_policy": counts.no_policy,
        "no_allocation_in_policy": counts.no_allocation_in_policy,
        "skipped_own": counts.skipped_own,
        "nothing_due": counts.nothing_due,
    }
