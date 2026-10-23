"""Clocking in and out, breaks, and attendance views (docs/product-requirements.md §6.4).

- Employees record their own events (`attendance.clock.self`). The time is the server's
  (ATT-2); the client's own clock is kept only as metadata. Events must follow a valid
  sequence (ATT-3), checked under a per-employee lock so two requests cannot both pass.
  Each request carries an `Idempotency-Key`; a repeat of the same key and event returns the
  current state instead of recording twice, and the same key for a different event is refused.
  Events voided by an approved correction no longer count.
- The work date of an event is the date its session started on: events of an open session
  keep that date, so a session crossing midnight stays on one day. A clock-in before the end
  of the previous day's shift, when that shift crosses midnight, belongs to that shift's date
  (ATT-9: the work date is the date the shift starts). Without a shift it is the date in the
  employee's location time zone.
- When the attendance policy effective on the work date does not allow several sessions, a
  second clock-in on the same work date is refused.
- Events are never changed or deleted (CLAUDE.md rule 5). A forgotten clock-out stays open
  until the employee clocks out or a correction closes it; the system never invents one (ATT-6).
- Reading needs `attendance.read` within scope (own, team, all). Each day is judged by
  `derive.derive_day` with the shift and policy effective on it (`days.judge`); a day without
  a configured shift and policy is shown with its times only.
"""

import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Final
from zoneinfo import ZoneInfo

from app.modules.attendance import location_rules, locations, timeline
from app.modules.attendance.days import MAX_RANGE_DAYS, UTC, JudgedDay, judge, voided_ids
from app.modules.attendance.models import AttendanceEvent, AttendanceEventVoid, EventSource
from app.modules.attendance.schedule import Schedule
from app.modules.attendance.timeline import EventType, SequenceError, State
from app.modules.org import public as org
from app.modules.people import public as people
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock
from app.platform.db import Database, Session
from app.platform.errors import ProblemError, ProblemType
from app.platform.ratelimit import RateLimiter, RateLimitRule, RateLimitScope

# docs/api-architecture.md §9.
CLOCK_EVENTS_PER_USER: Final = RateLimitRule(
    RateLimitScope.API, "attendance.events", 10, timedelta(minutes=1)
)
# A report over a period judges every day for everyone on the page.
MAX_REPORT_DAYS: Final = 62

_SEQUENCE_CODES: Final[dict[tuple[State, EventType], tuple[str, str]]] = {
    (State.CLOCKED_IN, EventType.CLOCK_IN): ("attendance.already_clocked_in", "You are already clocked in."),
    (State.ON_BREAK, EventType.CLOCK_IN): ("attendance.already_clocked_in", "You are already clocked in."),
    (State.CLOCKED_OUT, EventType.BREAK_START): (
        "attendance.not_clocked_in",
        "Clock in before starting a break.",
    ),
    (State.ON_BREAK, EventType.BREAK_START): ("attendance.already_on_break", "You are already on a break."),
    (State.CLOCKED_OUT, EventType.BREAK_END): ("attendance.not_clocked_in", "You are not clocked in."),
    (State.CLOCKED_IN, EventType.BREAK_END): ("attendance.not_on_break", "You are not on a break."),
    (State.CLOCKED_OUT, EventType.CLOCK_OUT): ("attendance.not_clocked_in", "You are not clocked in."),
}


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def _check_range(start: date, end: date, limit: int) -> None:
    if end < start:
        raise _field("to", "range.reversed", "The end date must be on or after the start date.")
    if (end - start).days >= limit:
        raise _field("to", "range.too_long", f"Choose a range of at most {limit} days.")


@dataclass(frozen=True, slots=True)
class Today:
    state: State
    work_date: date
    time_zone: str
    server_time: datetime
    allowed: frozenset[EventType]
    # Set when an open session started on an earlier day (a forgotten clock-out).
    open_since: date | None
    day: JudgedDay
    # Whether the next clock press is checked against an office (ADR-031), and the outcome of
    # each of the day's events that was (the person's own; never distances).
    location_check: bool = False
    event_locations: dict[uuid.UUID, locations.EventLocation] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TeamMember:
    colleague: people.Colleague
    state: State
    work_date: date
    totals: timeline.Totals
    last_event_at: datetime | None
    time_zone: str
    # Set when the open session started on an earlier work date (a forgotten clock-out): its
    # totals then stop at the last recorded event, because the departure time is unknown.
    open_since: date | None


@dataclass(frozen=True, slots=True)
class TeamSummary:
    in_scope: int
    clocked_in_today: int
    working_now: int
    on_break: int


@dataclass(frozen=True, slots=True)
class TeamToday:
    summary: TeamSummary
    members: list[TeamMember]
    next_after: tuple[str, uuid.UUID] | None


@dataclass(frozen=True, slots=True)
class RegisterEntry:
    colleague: people.Colleague
    day: JudgedDay


@dataclass(frozen=True, slots=True)
class Register:
    on: date
    in_scope: int
    # Status -> people; "not_judged" for days without a status yet or without configuration.
    counts: dict[str, int]
    entries: list[RegisterEntry]
    next_after: tuple[str, uuid.UUID] | None


@dataclass(slots=True)
class PeriodTotals:
    colleague: people.Colleague
    days: Counter[str] = field(default_factory=Counter)
    effective_minutes: int = 0
    late_days: int = 0
    late_minutes: int = 0
    early_leave_days: int = 0
    overtime_minutes: int = 0
    not_judged: int = 0


@dataclass(frozen=True, slots=True)
class PeriodReport:
    start: date
    end: date
    rows: list[PeriodTotals]
    next_after: tuple[str, uuid.UUID] | None


def totals_for(days: Sequence[JudgedDay], colleague: people.Colleague) -> PeriodTotals:
    row = PeriodTotals(colleague)
    for day in days:
        summary = day.summary
        if summary.status is None:
            if summary.configured or day.events:
                row.not_judged += 1
            continue
        row.days[summary.status.value] += 1
        row.effective_minutes += summary.effective_minutes
        if summary.late_minutes:
            row.late_days += 1
            row.late_minutes += summary.late_minutes
        if summary.early_leave_minutes:
            row.early_leave_days += 1
        row.overtime_minutes += summary.overtime_minutes
    return row


class AttendanceService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        authorizer: Authorizer,
        rate_limiter: RateLimiter,
        location_checks_enabled: bool = False,
    ) -> None:
        self._db = database
        self._clock = clock
        self._authz = authorizer
        self._limiter = rate_limiter
        # `LOCATION_CHECKS_ENABLED` (ADR-031): while off, no office check applies to any event.
        self._location_checks = location_checks_enabled

    # --- own day -------------------------------------------------------------------------------

    async def today(self, actor: Actor) -> Today:
        employee_id = _own_employee(actor)
        async with self._db.unit_of_work() as session:
            await self._authz.require(session, actor, "attendance.read.self", subject_employee_id=employee_id)
            return await self._today(session, employee_id)

    async def _today(self, session: Session, employee_id: uuid.UUID) -> Today:
        now = self._clock.now()
        zone_name, zone = await self._time_zone(session, employee_id)
        local_today = timeline.local_date(now, zone)
        last = await _latest_event(session, employee_id)
        state = timeline.state_after(EventType(last.event_type) if last else None)
        work_date = (
            last.work_date
            if last is not None and state is not State.CLOCKED_OUT
            else await self._new_work_date(session, employee_id, now, zone)
        )
        # A session left open from an earlier day is a forgotten clock-out, unless it is last
        # night's shift that has not ended yet.
        open_since = None
        if state is not State.CLOCKED_OUT and work_date < local_today:
            overnight = work_date == local_today - timedelta(days=1) and await self._within_previous_shift(
                session, employee_id, now, zone
            )
            open_since = None if overnight else work_date
        judged = await judge(session, [employee_id], work_date, work_date, now)
        day = judged[employee_id][0]
        return Today(
            state=state,
            work_date=work_date,
            time_zone=zone_name,
            server_time=now,
            allowed=timeline.ALLOWED[state],
            open_since=open_since,
            day=day,
            location_check=self._location_checks
            and bool(await locations.eligible_offices(session, employee_id, work_date)),
            event_locations=await locations.outcomes_of(session, [item.event.id for item in day.events]),
        )

    async def _within_previous_shift(
        self, session: Session, employee_id: uuid.UUID, now: datetime, zone: ZoneInfo
    ) -> bool:
        """Whether `now` is before the end of yesterday's shift, when that shift crosses midnight."""
        yesterday = timeline.local_date(now, zone) - timedelta(days=1)
        schedule = await Schedule.load(session, [employee_id], yesterday, yesterday)
        return _overnight_in_progress(schedule, employee_id, yesterday, now, zone)

    @staticmethod
    async def _forgotten_clock_outs(
        session: Session,
        open_from: dict[uuid.UUID, date],
        local_today: dict[uuid.UUID, date],
        zones: Mapping[uuid.UUID, tzinfo],
        now: datetime,
    ) -> dict[uuid.UUID, date]:
        """Of sessions still open from an earlier work date, those that are forgotten
        clock-outs: every one except last night's shift crossing midnight that has not ended
        yet (the same rule as one's own day)."""
        if not open_from:
            return {}
        schedule = await Schedule.load(
            session, list(open_from), min(open_from.values()), max(open_from.values())
        )
        return {
            employee_id: work_date
            for employee_id, work_date in open_from.items()
            if not (
                work_date == local_today[employee_id] - timedelta(days=1)
                and _overnight_in_progress(schedule, employee_id, work_date, now, zones[employee_id])
            )
        }

    async def _new_work_date(
        self, session: Session, employee_id: uuid.UUID, now: datetime, zone: ZoneInfo
    ) -> date:
        """The work date a new session starting now belongs to (ATT-9)."""
        local_today = timeline.local_date(now, zone)
        if await self._within_previous_shift(session, employee_id, now, zone):
            return local_today - timedelta(days=1)
        return local_today

    async def record(
        self,
        actor: Actor,
        context: RequestContext,
        *,
        event_type: EventType,
        idempotency_key: str,
        client_reported_at: datetime | None,
        location: location_rules.Position | location_rules.Reason | None = None,
    ) -> Today:
        """`location` is what the browser reported for this press (ADR-031); it is judged in
        this transaction, never stored as coordinates, and never stops the event."""
        employee_id = _own_employee(actor)
        await self._limiter.enforce(CLOCK_EVENTS_PER_USER, str(actor.user_id))
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "attendance.clock.self", subject_employee_id=employee_id
            )
            # One writer per employee at a time, so the sequence check and the insert agree.
            await session.lock(f"attendance:{employee_id}")
            replayed = await session.find_one(
                AttendanceEvent, {"employee_id": employee_id, "idempotency_key": idempotency_key}
            )
            if replayed is not None:
                if replayed.event_type != event_type.value:
                    raise _field(
                        "Idempotency-Key",
                        "idempotency.key_reused",
                        "This request key was already used for a different action. Reload and try again.",
                    )
                return await self._today(session, employee_id)
            _, zone = await self._time_zone(session, employee_id)
            last = await _latest_event(session, employee_id)
            state = timeline.state_after(EventType(last.event_type) if last else None)
            try:
                timeline.next_state(state, event_type)
            except SequenceError as exc:
                code, detail = _SEQUENCE_CODES[(exc.state, exc.event_type)]
                raise _conflict(code, detail) from exc
            if last is not None and state is not State.CLOCKED_OUT:
                work_date = last.work_date
            else:
                work_date = await self._new_work_date(session, employee_id, now, zone)
                await self._check_single_session(session, employee_id, work_date)
            event = AttendanceEvent(
                employee_id=employee_id,
                event_type=event_type.value,
                occurred_at=now,
                work_date=work_date,
                source=EventSource.WEB.value,
                recorded_by=actor.user_id,
                client_reported_at=client_reported_at,
                ip=str(context.ip) if context.ip is not None else None,
                user_agent=context.user_agent,
                idempotency_key=idempotency_key,
            )
            session.add(event)
            await session.flush()
            # After every attendance rule passed: the evidence never decides whether the event
            # is recorded.
            if self._location_checks:
                await locations.record_evidence(session, event, location, now)
                await session.flush()
            return await self._today(session, employee_id)

    async def _check_single_session(self, session: Session, employee_id: uuid.UUID, work_date: date) -> None:
        """Refuse a second session on a work date whose policy allows only one."""
        schedule = await Schedule.load(session, [employee_id], work_date, work_date)
        applied = schedule.applied(employee_id, work_date)
        if applied is None or applied.policy is None or applied.policy.allow_multiple_sessions:
            return
        clock_outs = await session.find(
            AttendanceEvent,
            {"employee_id": employee_id, "work_date": work_date, "event_type": EventType.CLOCK_OUT.value},
        )
        if await _unvoided(session, clock_outs):
            raise _conflict(
                "attendance.single_session",
                "You already clocked out today, and your attendance policy allows one session a day. "
                "If you clocked out by mistake, ask for a correction.",
            )

    # --- history -------------------------------------------------------------------------------

    async def days(
        self, actor: Actor, employee_id: uuid.UUID | None, start: date, end: date
    ) -> tuple[list[JudgedDay], str, dict[uuid.UUID, locations.EventLocation]]:
        """Days in the range (inclusive) with events, a status, leave or a holiday, newest
        first, the employee's time zone, and the location outcome of their events when the
        reader may see them (the person, or a location reviewer covering them). Days after
        today are left out."""
        _check_range(start, end, MAX_RANGE_DAYS)
        subject = employee_id or _own_employee(actor)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "attendance.read", subject_employee_id=subject, hide_existence=True
            )
            zone_name, zone = await self._time_zone(session, subject, required=False)
            last = min(end, timeline.local_date(now, zone))
            if last < start:
                return [], zone_name, {}
            judged = await judge(session, [subject], start, last, now)
            days = [day for day in judged.get(subject, []) if day.notable]
            outcomes = (
                await locations.outcomes_of(session, [item.event.id for day in days for item in day.events])
                if await locations.may_see_outcomes(session, self._authz, actor, subject)
                else {}
            )
        return sorted(days, key=lambda day: day.work_date, reverse=True), zone_name, outcomes

    # --- team ----------------------------------------------------------------------------------

    async def team_today(
        self,
        actor: Actor,
        *,
        text_query: str | None,
        department_id: uuid.UUID | None,
        location_id: uuid.UUID | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> TeamToday:
        now = self._clock.now()
        on = self._authz.today()
        async with self._db.unit_of_work() as session:
            scope = await self._authz.scope_filter(
                session, actor, "attendance.read", people.EMPLOYEE_ID_FIELD
            )
            everyone, _ = await people.employed_colleagues(session, scope, on=on)
            page, next_after = await people.employed_colleagues(
                session,
                scope,
                on=on,
                text=text_query,
                department_id=department_id,
                location_id=location_id,
                after=after,
                limit=limit,
            )
            zones = await org.location_time_zones(
                session, {c.location_id for c in everyone if c.location_id is not None}
            )
            local_today = {
                c.id: timeline.local_date(
                    now, ZoneInfo(zones[c.location_id]) if c.location_id in zones else UTC
                )
                for c in everyone
            }
            ids = list(local_today)
            latest = await _latest_events(session, ids)
            earliest_today = min(local_today.values(), default=on)
            clock_ins = {
                (event.employee_id, event.work_date)
                for event in await _unvoided(
                    session,
                    await session.find(
                        AttendanceEvent,
                        {
                            "employee_id": {"$in": ids},
                            "event_type": EventType.CLOCK_IN.value,
                            "work_date": {"$gte": earliest_today},
                        },
                    ),
                )
            }
            states = {
                employee_id: timeline.state_after(EventType(event.event_type))
                for employee_id, event in latest.items()
            }
            work_dates = {
                c.id: latest[c.id].work_date
                if c.id in latest and states[c.id] is not State.CLOCKED_OUT
                else local_today[c.id]
                for c in page
            }
            # Over the whole scope, so the counts leave forgotten clock-outs out too.
            forgotten = await self._forgotten_clock_outs(
                session,
                {
                    employee_id: event.work_date
                    for employee_id, event in latest.items()
                    if states[employee_id] is not State.CLOCKED_OUT
                    and event.work_date < local_today[employee_id]
                },
                local_today,
                {c.id: ZoneInfo(zones[c.location_id]) if c.location_id in zones else UTC for c in everyone},
                now,
            )
            page_rows = await _events_on(session, work_dates)
        summary = TeamSummary(
            in_scope=len(everyone),
            clocked_in_today=len(
                {
                    employee_id
                    for employee_id, work_date in clock_ins
                    if work_date == local_today.get(employee_id)
                }
            ),
            # A session left open from an earlier day is a forgotten clock-out, not work now.
            working_now=sum(
                state is not State.CLOCKED_OUT and employee_id not in forgotten
                for employee_id, state in states.items()
            ),
            on_break=sum(
                state is State.ON_BREAK and employee_id not in forgotten
                for employee_id, state in states.items()
            ),
        )
        members = [
            TeamMember(
                colleague=c,
                state=states.get(c.id, State.CLOCKED_OUT),
                work_date=work_dates[c.id],
                # A forgotten clock-out counts nothing past the last event: the session is not
                # stretched to now, so it never reads as hours worked or overtime.
                totals=timeline.totals(
                    _events(page_rows.get(c.id, [])),
                    latest[c.id].occurred_at if c.id in forgotten else now,
                ),
                last_event_at=latest[c.id].occurred_at if c.id in latest else None,
                time_zone=zones.get(c.location_id, "UTC") if c.location_id else "UTC",
                open_since=forgotten.get(c.id),
            )
            for c in page
        ]
        return TeamToday(summary, members, next_after)

    async def register(
        self,
        actor: Actor,
        *,
        on: date,
        text_query: str | None,
        department_id: uuid.UUID | None,
        location_id: uuid.UUID | None,
        status: str | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> Register:
        """Everyone in the reader's scope who was employed on the date, with that day judged,
        and counts per status over the whole scope (the daily attendance register)."""
        if on > self._authz.today() + timedelta(days=1):
            raise _field("date", "attendance.future_date", "Choose a date that has already started.")
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            scope = await self._authz.scope_filter(
                session, actor, "attendance.read", people.EMPLOYEE_ID_FIELD, on=on
            )
            everyone, _ = await people.employed_colleagues(
                session,
                scope,
                on=on,
                since=on,
                text=text_query,
                department_id=department_id,
                location_id=location_id,
            )
            judged = await judge(session, [c.id for c in everyone], on, on, now)
        entries = [RegisterEntry(c, judged[c.id][0]) for c in everyone]
        counts = Counter(
            entry.day.summary.status.value if entry.day.summary.status else "not_judged" for entry in entries
        )
        if status is not None:
            entries = [
                e
                for e in entries
                if (e.day.summary.status.value if e.day.summary.status else "not_judged") == status
            ]
        if after is not None:
            entries = [
                e
                for e in entries
                if (e.colleague.name.lower(), e.colleague.id) > (after[0].lower(), after[1])
            ]
        page = entries[:limit]
        last = page[-1] if len(entries) > limit else None
        return Register(
            on=on,
            in_scope=len(everyone),
            counts=dict(counts),
            entries=page,
            next_after=(last.colleague.name, last.colleague.id) if last else None,
        )

    async def report(
        self,
        actor: Actor,
        *,
        start: date,
        end: date,
        text_query: str | None,
        department_id: uuid.UUID | None,
        location_id: uuid.UUID | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int | None,
        employee_ids: Sequence[uuid.UUID] | None = None,
    ) -> PeriodReport:
        """Per person in scope employed at some point in the period: days per status and the
        period's worked, late and overtime totals (the attendance report, RPT-1)."""
        _check_range(start, end, MAX_REPORT_DAYS)
        today = self._authz.today()
        if start > today:
            raise _field("from", "attendance.future_date", "Choose a period that has already started.")
        end = min(end, today)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            if employee_ids is not None:
                scope = {people.EMPLOYEE_ID_FIELD: {"$in": list(employee_ids)}}
            else:
                scope = await self._authz.scope_filter(
                    session, actor, "attendance.read", people.EMPLOYEE_ID_FIELD, on=end
                )
            page, next_after = await people.employed_colleagues(
                session,
                scope,
                on=end,
                since=start,
                text=text_query,
                department_id=department_id,
                location_id=location_id,
                after=after,
                limit=limit,
            )
            judged = await judge(session, [c.id for c in page], start, end, now)
        rows = [totals_for(judged.get(c.id, []), c) for c in page]
        return PeriodReport(start, end, rows, next_after)

    # --- helpers -------------------------------------------------------------------------------

    async def _time_zone(
        self, session: Session, employee_id: uuid.UUID, *, required: bool = True
    ) -> tuple[str, ZoneInfo]:
        placement = await people.placement(session, employee_id, self._authz.today())
        zones = await org.location_time_zones(session, [placement.location_id]) if placement else {}
        if placement is None or placement.location_id not in zones:
            if required:
                raise _conflict(
                    "attendance.no_current_job",
                    "Your employee record has no current job and location, so attendance cannot be "
                    "recorded. Contact HR.",
                )
            return "UTC", UTC
        name = zones[placement.location_id]
        return name, ZoneInfo(name)


def _own_employee(actor: Actor) -> uuid.UUID:
    if actor.employee_id is None:
        raise ProblemError(ProblemType.NOT_FOUND, detail="Your account is not linked to an employee record.")
    return actor.employee_id


def _events(rows: Sequence[AttendanceEvent]) -> list[timeline.Event]:
    return [timeline.Event(EventType(row.event_type), row.occurred_at) for row in rows]


async def _unvoided(session: Session, events: Sequence[AttendanceEvent]) -> list[AttendanceEvent]:
    """The events no approved correction voided (voids are kept apart from the events)."""
    voided = await voided_ids(session, [event.id for event in events])
    return [event for event in events if event.id not in voided]


async def _latest_event(session: Session, employee_id: uuid.UUID) -> AttendanceEvent | None:
    return (await _latest_events(session, [employee_id])).get(employee_id)


async def _latest_events(
    session: Session, employee_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, AttendanceEvent]:
    """Each employee's latest event that no correction voided."""
    if not employee_ids:
        return {}
    pipeline = [
        {"$match": {"employee_id": {"$in": list(employee_ids)}}},
        session.tenant_lookup(AttendanceEventVoid, local="_id", foreign="event_id", into="void"),
        {"$match": {"void": {"$size": 0}}},
        {"$sort": {"employee_id": 1, "occurred_at": -1, "_id": -1}},
        {"$group": {"_id": "$employee_id", "event": {"$first": "$_id"}}},
    ]
    latest_ids = [row["event"] for row in await session.aggregate(AttendanceEvent, pipeline)]
    events = await session.find(AttendanceEvent, {"id": {"$in": latest_ids}}) if latest_ids else []
    return {event.employee_id: event for event in events}


async def _events_on(
    session: Session, work_dates: dict[uuid.UUID, date]
) -> dict[uuid.UUID, list[AttendanceEvent]]:
    if not work_dates:
        return {}
    events = await session.find(
        AttendanceEvent,
        {
            "employee_id": {"$in": list(work_dates)},
            "work_date": {"$gte": min(work_dates.values()), "$lte": max(work_dates.values())},
        },
        sort=[("occurred_at", 1), ("id", 1)],
    )
    found: dict[uuid.UUID, list[AttendanceEvent]] = defaultdict(list)
    for event in await _unvoided(session, events):
        if event.work_date == work_dates[event.employee_id]:
            found[event.employee_id].append(event)
    return found


def _overnight_in_progress(
    schedule: Schedule, employee_id: uuid.UUID, work_date: date, now: datetime, zone: tzinfo
) -> bool:
    """Whether `now` is before the end of the work date's shift, when that shift crosses midnight."""
    found = schedule.shift_on(employee_id, work_date)
    if found is None or not found[1].crosses_midnight:
        return False
    return now < datetime.combine(work_date + timedelta(days=1), found[1].end_time, zone)
