"""The leave year: periodic accrual, carried-day expiry, the year-end run and opening-balance
imports (product-requirements LV-1, LV-2, §6.12; roadmap M4 exit).

Every amount and rule comes from the policy effective for the employee (CLAUDE.md rule 18);
policies that leave a rule undecided are skipped and counted, never filled in.

- Periodic accrual (worker, daily, as the system): for the current and the previous leave
  year, each period whose credit date (its first or last day, per the policy) has come is
  credited once per employee employed then, with the amount of the policy effective on that
  date, prorated on joining and exit when the policy says so. The ledger's unique index makes
  a rerun or a second worker harmless.
- Carried-day expiry (worker, daily, as the system): carried days not used within the
  policy's months of the new leave year expire once (`carry_forward_expiry`).
- The year-end run (HR, `leave.balance.adjust`, never on one's own balance, SOD-6): for a
  leave type and the last day of a leave year, each employee's closing balance is carried
  (`carry_forward_out` and `carry_forward`) up to the policy's cap, and the rest lapses
  (`lapse`) or stays. A preview records nothing; applying writes each employee at most once.
- Opening balances (HR, `leave.balance.adjust`, SOD-6): rows of employee code, leave type
  code, days and date are checked one by one; applying records them as one import batch,
  each at most once per employee, leave type and leave year.
"""

import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

import procrastinate
from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leave.models import (
    PERIODIC_WHERE,
    YEARLY_WHERE,
    AccrualMethod,
    EntryType,
    LeaveLedgerEntry,
    LeavePolicy,
    LeaveType,
    LeaveTypeStatus,
)
from app.modules.leave.rules import (
    accrual_periods,
    add_months,
    expired_carry,
    leave_year_start,
    most_specific,
    next_leave_year_start,
    prorated,
    year_end,
)
from app.modules.people import public as people
from app.platform.audit.records import AuditActor, AuditEvent, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType

logger = logging.getLogger(__name__)

ACCRUAL_TASK: Final = "leave.accrue"
# Daily, early in the UTC day.
ACCRUAL_SCHEDULE: Final = "15 0 * * *"
QUEUE: Final = "leave"
ZERO: Final = Decimal("0.00")


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


async def _lock_employee(session: AsyncSession, employee_id: uuid.UUID) -> None:
    """The lock every change to one employee's leave takes (service.py)."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"leave:{employee_id}"}
    )


async def _policy_on(
    session: AsyncSession, leave_type_id: uuid.UUID, employee_id: uuid.UUID, on: date
) -> tuple[LeavePolicy | None, people.JobFacts | None]:
    facts = await people.job_facts(session, employee_id, on)
    if facts is None:
        return None, None
    candidates = list(
        await session.scalars(
            select(LeavePolicy).where(
                LeavePolicy.leave_type_id == leave_type_id,
                LeavePolicy.effective_from <= on,
                or_(LeavePolicy.effective_to.is_(None), LeavePolicy.effective_to > on),
            )
        )
    )
    return most_specific(candidates, facts.location_id, facts.employment_type), facts


async def _sum(session: AsyncSession, *conditions: ColumnElement[bool]) -> Decimal:
    value = (
        await session.execute(select(func.coalesce(func.sum(LeaveLedgerEntry.amount), 0)).where(*conditions))
    ).scalar_one()
    return Decimal(value)


# --- the worker's daily run -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccrualResult:
    accrued: int = 0
    expired: int = 0


async def accrue_due(database: Database, clock: Clock, audit: AuditWriter) -> AccrualResult:
    """Credit every due periodic accrual and expire every due carried balance, today."""
    today = clock.now().date()
    since = today - timedelta(days=400)
    accrued = expired = 0
    async with database.unit_of_work() as session:
        # Only leave types with a periodic policy in force during the last two leave years.
        periodic = list(
            await session.scalars(
                select(LeavePolicy.leave_type_id)
                .join(LeaveType, LeaveType.id == LeavePolicy.leave_type_id)
                .where(
                    LeaveType.status == LeaveTypeStatus.ACTIVE.value,
                    LeavePolicy.accrual_method == AccrualMethod.PERIODIC.value,
                    or_(
                        LeavePolicy.effective_to.is_(None),
                        LeavePolicy.effective_to > since - timedelta(days=366),
                    ),
                )
                .distinct()
            )
        )
        employed = (
            (
                await people.employed_colleagues(
                    session, people.employee_id_column().is_not(None), on=today, since=since
                )
            )[0]
            if periodic
            else []
        )
        # Only balances that carried days into a leave year.
        carried = (
            await session.execute(
                select(LeaveLedgerEntry.employee_id, LeaveLedgerEntry.leave_type_id)
                .where(LeaveLedgerEntry.entry_type == EntryType.CARRY_FORWARD.value)
                .distinct()
            )
        ).all()
    for leave_type_id in periodic:
        for colleague in employed:
            async with database.unit_of_work() as session:
                await _lock_employee(session, colleague.id)
                accrued += await _accrue_employee(session, leave_type_id, colleague.id, today)
    for employee_id, leave_type_id in carried:
        async with database.unit_of_work() as session:
            await _lock_employee(session, employee_id)
            expired += await _expire_employee(session, leave_type_id, employee_id, today)
    result = AccrualResult(accrued, expired)
    if result != AccrualResult():
        async with database.unit_of_work() as session:
            await audit.record(
                session,
                AuditEvent(
                    action="leave.accrual.run",
                    actor=AuditActor.job(),
                    changes={
                        "accrued_entries": ValueChange(None, accrued),
                        "expired_entries": ValueChange(None, expired),
                        "run_date": ValueChange(None, today),
                    },
                ),
            )
    return result


async def _accrue_employee(
    session: AsyncSession, leave_type_id: uuid.UUID, employee_id: uuid.UUID, today: date
) -> int:
    policy, _ = await _policy_on(session, leave_type_id, employee_id, today)
    if policy is None or policy.accrual_method != AccrualMethod.PERIODIC.value:
        return 0
    current = leave_year_start(today, policy.leave_year_start_month, policy.leave_year_start_day)
    previous = date(current.year - 1, current.month, current.day)
    credited = set(
        await session.scalars(
            select(LeaveLedgerEntry.period_start).where(
                LeaveLedgerEntry.employee_id == employee_id,
                LeaveLedgerEntry.leave_type_id == leave_type_id,
                LeaveLedgerEntry.entry_type == EntryType.ACCRUAL.value,
                LeaveLedgerEntry.period_start >= previous,
            )
        )
    )
    written = 0
    for year in (previous, current):
        for period in accrual_periods(year, policy.accrual_frequency or "monthly"):
            credit = period.start if policy.accrual_timing == "period_start" else period.end
            if credit > today or period.start in credited:
                continue
            on_policy, facts = await _policy_on(session, leave_type_id, employee_id, credit)
            if (
                on_policy is None
                or facts is None
                or on_policy.accrual_method != AccrualMethod.PERIODIC.value
                or on_policy.accrual_amount is None
            ):
                continue
            amount = prorated(
                on_policy.accrual_amount,
                period,
                facts.date_of_joining,
                facts.date_of_exit,
                on_joining=bool(on_policy.prorate_on_joining),
                on_exit=bool(on_policy.prorate_on_exit),
                rounding=on_policy.proration_rounding,
            )
            if amount <= 0:
                continue
            inserted = (
                await session.execute(
                    insert(LeaveLedgerEntry)
                    .values(
                        employee_id=employee_id,
                        leave_type_id=leave_type_id,
                        leave_year=year,
                        entry_type=EntryType.ACCRUAL.value,
                        amount=amount,
                        effective_date=credit,
                        period_start=period.start,
                        policy_id=on_policy.id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            LeaveLedgerEntry.employee_id,
                            LeaveLedgerEntry.leave_type_id,
                            LeaveLedgerEntry.period_start,
                        ],
                        index_where=text(PERIODIC_WHERE),
                    )
                    .returning(LeaveLedgerEntry.id)
                )
            ).scalar_one_or_none()
            written += inserted is not None
    return written


async def _expire_employee(
    session: AsyncSession, leave_type_id: uuid.UUID, employee_id: uuid.UUID, today: date
) -> int:
    carries = list(
        await session.scalars(
            select(LeaveLedgerEntry).where(
                LeaveLedgerEntry.employee_id == employee_id,
                LeaveLedgerEntry.leave_type_id == leave_type_id,
                LeaveLedgerEntry.entry_type == EntryType.CARRY_FORWARD.value,
            )
        )
    )
    written = 0
    for carry in carries:
        policy = await session.get(LeavePolicy, carry.policy_id) if carry.policy_id else None
        if policy is None or policy.carried_expire_after_months is None:
            continue
        expires_on = add_months(carry.leave_year, policy.carried_expire_after_months)
        if expires_on > today:
            continue
        used = -await _sum(
            session,
            LeaveLedgerEntry.employee_id == employee_id,
            LeaveLedgerEntry.leave_type_id == leave_type_id,
            LeaveLedgerEntry.entry_type.in_([EntryType.USAGE.value, EntryType.USAGE_REVERSAL.value]),
            LeaveLedgerEntry.effective_date >= carry.leave_year,
            LeaveLedgerEntry.effective_date < expires_on,
        )
        balance = await _sum(
            session,
            LeaveLedgerEntry.employee_id == employee_id,
            LeaveLedgerEntry.leave_type_id == leave_type_id,
            LeaveLedgerEntry.effective_date < expires_on,
        )
        amount = expired_carry(carry.amount, used, balance)
        if amount <= 0:
            continue
        inserted = (
            await session.execute(
                insert(LeaveLedgerEntry)
                .values(
                    employee_id=employee_id,
                    leave_type_id=leave_type_id,
                    leave_year=carry.leave_year,
                    entry_type=EntryType.CARRY_FORWARD_EXPIRY.value,
                    amount=-amount,
                    effective_date=expires_on,
                    policy_id=policy.id,
                    reason="Carried days not used in time",
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
        written += inserted is not None
    return written


def register_leave_jobs(
    job_app: procrastinate.App, database: Database, clock: Clock, audit: AuditWriter
) -> None:
    """Daily, one run at a time (`api-architecture.md` §10, `leave.accrue`)."""

    @job_app.periodic(cron=ACCRUAL_SCHEDULE, periodic_id=ACCRUAL_TASK)
    @job_app.task(name=ACCRUAL_TASK, queue=QUEUE, lock=ACCRUAL_TASK)
    async def accrue(timestamp: int) -> None:
        result = await accrue_due(database, clock, audit)
        if result != AccrualResult():
            logger.info("leave.accrued", extra={"accrued": result.accrued, "expired": result.expired})


# --- HR: the year end and opening balances ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class YearEndRow:
    employee_id: uuid.UUID
    employee_name: str
    outcome: str
    closing: Decimal | None = None
    carried: Decimal | None = None
    lapsed: Decimal | None = None
    kept: Decimal | None = None


@dataclass(frozen=True, slots=True)
class YearEndReport:
    applied: bool
    rows: list[YearEndRow]

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(row.outcome for row in self.rows))


@dataclass(frozen=True, slots=True)
class ImportRow:
    line: int
    employee_code: str
    leave_type_code: str
    days: Decimal
    effective_date: date


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    line: int
    employee_code: str
    leave_type_code: str
    outcome: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    applied: bool
    batch_id: uuid.UUID | None
    rows: list[ImportOutcome]

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(row.outcome for row in self.rows))


class LeaveYearService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    async def close_year(
        self, actor: Actor, context: RequestContext, leave_type_id: uuid.UUID, last_day: date, *, apply: bool
    ) -> YearEndReport:
        if last_day >= self._authz.today():
            raise field_error(
                "year_end", "leave.year_not_ended", "Choose the last day of a leave year that has ended."
            )
        rows: list[YearEndRow] = []
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "leave.balance.adjust")
            leave_type = await session.get(LeaveType, leave_type_id)
            if leave_type is None:
                raise field_error("leave_type_id", "leave.unknown_type", "Choose an existing leave type.")
            colleagues, _ = await people.employed_colleagues(
                session, people.employee_id_column().is_not(None), on=last_day
            )
            for colleague in colleagues:
                rows.append(
                    await self._close_one(session, actor, colleague, leave_type.id, last_day, apply=apply)
                )
            report = YearEndReport(apply, rows)
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.year_end.applied" if apply else "leave.year_end.previewed",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_type",
                    target_id=leave_type.id,
                    changes={
                        "year_end": ValueChange(None, last_day),
                        **{
                            f"count_{name}": ValueChange(None, value)
                            for name, value in sorted(report.counts.items())
                        },
                    },
                ),
            )
        return report

    async def _close_one(  # noqa: PLR0911 (one outcome per reason an employee is not closed)
        self,
        session: AsyncSession,
        actor: Actor,
        colleague: people.Colleague,
        leave_type_id: uuid.UUID,
        last_day: date,
        *,
        apply: bool,
    ) -> YearEndRow:
        def row(outcome: str, **values: Decimal) -> YearEndRow:
            return YearEndRow(colleague.id, colleague.name, outcome, **values)

        try:
            await self._authz.require(
                session, actor, "leave.balance.adjust", subject_employee_id=colleague.id
            )
        except AccessDenied:
            return row("not_permitted")
        if colleague.id == actor.employee_id:
            return row("skipped_own")
        policy, _ = await _policy_on(session, leave_type_id, colleague.id, last_day)
        if policy is None:
            return row("no_policy")
        if policy.carry_forward_mode is None:
            return row("not_decided")
        year = leave_year_start(last_day, policy.leave_year_start_month, policy.leave_year_start_day)
        following = next_leave_year_start(year)
        if following - timedelta(days=1) != last_day:
            return row("not_year_end")
        await _lock_employee(session, colleague.id)
        same = (
            LeaveLedgerEntry.employee_id == colleague.id,
            LeaveLedgerEntry.leave_type_id == leave_type_id,
        )
        done = (
            await session.execute(
                select(LeaveLedgerEntry.id).where(
                    *same,
                    LeaveLedgerEntry.leave_year == year,
                    LeaveLedgerEntry.entry_type.in_(
                        [EntryType.CARRY_FORWARD_OUT.value, EntryType.LAPSE.value]
                    ),
                )
            )
        ).first()
        if done is not None:
            return row("already_closed")
        closing = await _sum(session, *same, LeaveLedgerEntry.leave_year <= year)
        result = year_end(
            closing,
            mode=policy.carry_forward_mode,
            cap=policy.carry_forward_cap,
            excess_action=policy.excess_action,
        )
        values = {
            "closing": result.closing,
            "carried": result.carried,
            "lapsed": result.lapsed,
            "kept": result.kept,
        }
        if not apply:
            return row("would_do", **values)
        reason = f"Year end {last_day.isoformat()}"
        entries = []
        if result.carried > 0:
            entries += [
                (EntryType.CARRY_FORWARD_OUT, -result.carried, year, last_day),
                (EntryType.CARRY_FORWARD, result.carried, following, following),
            ]
        if result.lapsed > 0:
            entries.append((EntryType.LAPSE, -result.lapsed, year, last_day))
        for entry_type, amount, leave_year, effective in entries:
            session.add(
                LeaveLedgerEntry(
                    employee_id=colleague.id,
                    leave_type_id=leave_type_id,
                    leave_year=leave_year,
                    entry_type=entry_type.value,
                    amount=amount,
                    effective_date=effective,
                    policy_id=policy.id,
                    reason=reason,
                    created_by=actor.user_id,
                )
            )
        await session.flush()
        return row("done", **values)

    async def import_opening_balances(
        self, actor: Actor, context: RequestContext, rows: Sequence[ImportRow], reason: str, *, apply: bool
    ) -> ImportReport:
        """Check every row; when asked to apply and every row is fine, record them all as one
        batch. A file with any refused row records nothing, so it can be fixed and sent again."""
        seen: set[tuple[str, str, date]] = set()
        checked: list[tuple[ImportRow, str, _Planned | None]] = []
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "leave.balance.adjust")
            types = {
                code.lower(): type_id
                for type_id, code in (await session.execute(select(LeaveType.id, LeaveType.code))).all()
            }
            employees = await people.employee_ids_by_code(session, {row.employee_code for row in rows})
            for row in rows:
                checked.append(
                    await self._check_row(session, actor, row, types=types, employees=employees, seen=seen)
                )
            write = apply and all(outcome == "ok" for _, outcome, _ in checked)
            batch_id = uuid.uuid4() if write else None
            if write:
                session.add_all(
                    LeaveLedgerEntry(
                        employee_id=planned.employee_id,
                        leave_type_id=planned.leave_type_id,
                        leave_year=planned.leave_year,
                        entry_type=EntryType.OPENING_BALANCE.value,
                        amount=planned.days,
                        effective_date=planned.effective_date,
                        policy_id=planned.policy_id,
                        reason=reason,
                        created_by=actor.user_id,
                        import_batch_id=batch_id,
                    )
                    for _, _, planned in checked
                    if planned is not None
                )
                await session.flush()
            outcomes = [
                ImportOutcome(
                    row.line, row.employee_code, row.leave_type_code, "recorded" if write else outcome
                )
                for row, outcome, _ in checked
            ]
            report = ImportReport(write, batch_id, outcomes)
            await self._audit.record(
                session,
                AuditEvent(
                    action="leave.opening_balances.imported" if write else "leave.opening_balances.checked",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="leave_import",
                    target_id=batch_id or uuid.uuid4(),
                    changes={
                        "rows": ValueChange(None, len(rows)),
                        **{
                            f"count_{name}": ValueChange(None, value)
                            for name, value in sorted(report.counts.items())
                        },
                    },
                    reason=reason,
                ),
            )
        return report

    async def _check_row(
        self,
        session: AsyncSession,
        actor: Actor,
        row: ImportRow,
        *,
        types: dict[str, uuid.UUID],
        employees: dict[str, uuid.UUID],
        seen: set[tuple[str, str, date]],
    ) -> "tuple[ImportRow, str, _Planned | None]":
        key = (row.employee_code.lower(), row.leave_type_code.lower(), row.effective_date)
        outcome: str | None = None
        employee_id = employees.get(row.employee_code.lower())
        leave_type_id = types.get(row.leave_type_code.lower())
        if key in seen:
            outcome = "duplicate_row"
        elif row.days == 0:
            outcome = "zero_days"
        elif employee_id is None:
            outcome = "unknown_employee"
        elif leave_type_id is None:
            outcome = "unknown_leave_type"
        seen.add(key)
        if outcome is not None or employee_id is None or leave_type_id is None:
            return row, outcome or "unknown_employee", None
        try:
            await self._authz.require(session, actor, "leave.balance.adjust", subject_employee_id=employee_id)
        except AccessDenied:
            return row, "not_permitted", None
        if employee_id == actor.employee_id:
            return row, "own_balance", None
        policy, _ = await _policy_on(session, leave_type_id, employee_id, row.effective_date)
        if policy is None:
            return row, "no_policy", None
        year = leave_year_start(
            row.effective_date, policy.leave_year_start_month, policy.leave_year_start_day
        )
        await _lock_employee(session, employee_id)
        exists = (
            await session.execute(
                select(LeaveLedgerEntry.id).where(
                    LeaveLedgerEntry.employee_id == employee_id,
                    LeaveLedgerEntry.leave_type_id == leave_type_id,
                    LeaveLedgerEntry.leave_year == year,
                    LeaveLedgerEntry.entry_type == EntryType.OPENING_BALANCE.value,
                )
            )
        ).first()
        if exists is not None:
            return row, "already_recorded", None
        return row, "ok", _Planned(employee_id, leave_type_id, year, row.days, row.effective_date, policy.id)


@dataclass(frozen=True, slots=True)
class _Planned:
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    leave_year: date
    days: Decimal
    effective_date: date
    policy_id: uuid.UUID
