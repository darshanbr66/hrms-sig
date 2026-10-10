"""Clocking in and out, breaks, and attendance views (docs/product-requirements.md §6.4).

- Employees record their own events (`attendance.clock.self`). The time is the server's
  (ATT-2); the client's own clock is kept only as metadata. Events must follow a valid
  sequence (ATT-3), checked under a per-employee lock so two requests cannot both pass.
  Each request carries an `Idempotency-Key`; a repeat of the same key and event returns the
  current state instead of recording twice, and the same key for a different event is refused.
- The work date of an event is the date in the employee's location time zone when the session
  started: events of an open session keep its date, so a session crossing midnight stays on
  one day. (Shift-based work dates arrive with shifts.)
- Events are never changed or deleted (CLAUDE.md rule 5). A forgotten clock-out stays open
  until the employee clocks out; the system never invents one (ATT-6).
- Reading needs `attendance.read` within scope (own, team, all). Totals are clocked-in time
  and break time measured from the events, nothing more: whether a day counts as present,
  late or half a day depends on Sigvitas's shift and attendance policy, which this module does
  not apply until those are configured.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance import timeline
from app.modules.attendance.models import AttendanceEvent, EventSource
from app.modules.attendance.timeline import EventType, SequenceError, State
from app.modules.org import public as org
from app.modules.people import public as people
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.ratelimit import RateLimiter, RateLimitRule, RateLimitScope

# docs/api-architecture.md §9.
CLOCK_EVENTS_PER_USER: Final = RateLimitRule(
    RateLimitScope.API, "attendance.events", 10, timedelta(minutes=1)
)
MAX_RANGE_DAYS: Final = 93

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


@dataclass(frozen=True, slots=True)
class Day:
    work_date: date
    events: list[AttendanceEvent]
    totals: timeline.Totals


@dataclass(frozen=True, slots=True)
class Today:
    state: State
    work_date: date
    time_zone: str
    server_time: datetime
    allowed: frozenset[EventType]
    # Set when an open session started on an earlier day (a forgotten clock-out).
    open_since: date | None
    day: Day


@dataclass(frozen=True, slots=True)
class TeamMember:
    colleague: people.Colleague
    state: State
    work_date: date
    totals: timeline.Totals
    last_event_at: datetime | None


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


def _events(rows: Sequence[AttendanceEvent]) -> list[timeline.Event]:
    return [timeline.Event(EventType(row.event_type), row.occurred_at) for row in rows]


class AttendanceService:
    def __init__(
        self, *, database: Database, clock: Clock, authorizer: Authorizer, rate_limiter: RateLimiter
    ) -> None:
        self._db = database
        self._clock = clock
        self._authz = authorizer
        self._limiter = rate_limiter

    # --- own day -------------------------------------------------------------------------------

    async def today(self, actor: Actor) -> Today:
        employee_id = _own_employee(actor)
        async with self._db.unit_of_work() as session:
            await self._authz.require(session, actor, "attendance.read.self", subject_employee_id=employee_id)
            return await self._today(session, employee_id)

    async def _today(self, session: AsyncSession, employee_id: uuid.UUID) -> Today:
        now = self._clock.now()
        zone_name, zone = await self._time_zone(session, employee_id)
        local_today = timeline.local_date(now, zone)
        last = await _latest_event(session, employee_id)
        state = timeline.state_after(EventType(last.event_type) if last else None)
        work_date = last.work_date if last is not None and state is not State.CLOCKED_OUT else local_today
        rows = await _day_events(session, employee_id, work_date, work_date)
        return Today(
            state=state,
            work_date=work_date,
            time_zone=zone_name,
            server_time=now,
            allowed=timeline.ALLOWED[state],
            open_since=work_date if work_date < local_today else None,
            day=Day(work_date, rows, timeline.totals(_events(rows), now)),
        )

    async def record(
        self,
        actor: Actor,
        context: RequestContext,
        *,
        event_type: EventType,
        idempotency_key: str,
        client_reported_at: datetime | None,
    ) -> Today:
        employee_id = _own_employee(actor)
        await self._limiter.enforce(CLOCK_EVENTS_PER_USER, str(actor.user_id))
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "attendance.clock.self", subject_employee_id=employee_id
            )
            # One writer per employee at a time, so the sequence check and the insert agree.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"attendance:{employee_id}"},
            )
            replay = (
                await session.execute(
                    select(AttendanceEvent.event_type).where(
                        AttendanceEvent.employee_id == employee_id,
                        AttendanceEvent.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if replay is not None:
                if replay != event_type.value:
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
            work_date = (
                last.work_date
                if last is not None and state is not State.CLOCKED_OUT
                else timeline.local_date(now, zone)
            )
            session.add(
                AttendanceEvent(
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
            )
            await session.flush()
            return await self._today(session, employee_id)

    # --- history -------------------------------------------------------------------------------

    async def days(
        self, actor: Actor, employee_id: uuid.UUID | None, start: date, end: date
    ) -> tuple[list[Day], str]:
        """Days with events in the range (inclusive), newest first, and the employee's time zone."""
        if end < start:
            raise _field("to", "range.reversed", "The end date must be on or after the start date.")
        if (end - start).days >= MAX_RANGE_DAYS:
            raise _field("to", "range.too_long", f"Choose a range of at most {MAX_RANGE_DAYS} days.")
        subject = employee_id or _own_employee(actor)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            await self._authz.require(
                session, actor, "attendance.read", subject_employee_id=subject, hide_existence=True
            )
            zone_name, _ = await self._time_zone(session, subject, required=False)
            rows = await _day_events(session, subject, start, end)
        by_date: dict[date, list[AttendanceEvent]] = defaultdict(list)
        for row in rows:
            by_date[row.work_date].append(row)
        days = [
            Day(work_date, events, timeline.totals(_events(events), now))
            for work_date, events in sorted(by_date.items(), reverse=True)
        ]
        return days, zone_name

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
        scope = self._authz.scope_filter(actor, "attendance.read", people.employee_id_column())
        async with self._db.unit_of_work() as session:
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
                    now, ZoneInfo(zones[c.location_id]) if c.location_id in zones else _UTC
                )
                for c in everyone
            }
            ids = list(local_today)
            latest = await _latest_events(session, ids)
            earliest_today = min(local_today.values(), default=on)
            clock_ins = (
                await session.execute(
                    select(AttendanceEvent.employee_id, AttendanceEvent.work_date)
                    .where(
                        AttendanceEvent.employee_id.in_(ids),
                        AttendanceEvent.event_type == EventType.CLOCK_IN.value,
                        AttendanceEvent.work_date >= earliest_today,
                    )
                    .distinct()
                )
            ).all()
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
            page_rows = await _events_on(session, work_dates)
        summary = TeamSummary(
            in_scope=len(everyone),
            clocked_in_today=len(
                {row.employee_id for row in clock_ins if row.work_date == local_today.get(row.employee_id)}
            ),
            working_now=sum(state is not State.CLOCKED_OUT for state in states.values()),
            on_break=sum(state is State.ON_BREAK for state in states.values()),
        )
        members = [
            TeamMember(
                colleague=c,
                state=states.get(c.id, State.CLOCKED_OUT),
                work_date=work_dates[c.id],
                totals=timeline.totals(_events(page_rows.get(c.id, [])), now),
                last_event_at=latest[c.id].occurred_at if c.id in latest else None,
            )
            for c in page
        ]
        return TeamToday(summary, members, next_after)

    # --- helpers -------------------------------------------------------------------------------

    async def _time_zone(
        self, session: AsyncSession, employee_id: uuid.UUID, *, required: bool = True
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
            return "UTC", _UTC
        name = zones[placement.location_id]
        return name, ZoneInfo(name)


_UTC: Final = ZoneInfo("UTC")


def _own_employee(actor: Actor) -> uuid.UUID:
    if actor.employee_id is None:
        raise ProblemError(ProblemType.NOT_FOUND, detail="Your account is not linked to an employee record.")
    return actor.employee_id


async def _latest_event(session: AsyncSession, employee_id: uuid.UUID) -> AttendanceEvent | None:
    query = (
        select(AttendanceEvent)
        .where(AttendanceEvent.employee_id == employee_id)
        .order_by(AttendanceEvent.occurred_at.desc(), AttendanceEvent.id.desc())
        .limit(1)
    )
    return (await session.scalars(query)).first()


async def _latest_events(
    session: AsyncSession, employee_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, AttendanceEvent]:
    if not employee_ids:
        return {}
    query = (
        select(AttendanceEvent)
        .where(AttendanceEvent.employee_id.in_(employee_ids))
        .order_by(AttendanceEvent.employee_id, AttendanceEvent.occurred_at.desc(), AttendanceEvent.id.desc())
        .ext(distinct_on(AttendanceEvent.employee_id))
    )
    return {event.employee_id: event for event in await session.scalars(query)}


async def _day_events(
    session: AsyncSession, employee_id: uuid.UUID, start: date, end: date
) -> list[AttendanceEvent]:
    query = (
        select(AttendanceEvent)
        .where(
            AttendanceEvent.employee_id == employee_id,
            AttendanceEvent.work_date >= start,
            AttendanceEvent.work_date <= end,
        )
        .order_by(AttendanceEvent.occurred_at, AttendanceEvent.id)
    )
    return list(await session.scalars(query))


async def _events_on(
    session: AsyncSession, work_dates: dict[uuid.UUID, date]
) -> dict[uuid.UUID, list[AttendanceEvent]]:
    if not work_dates:
        return {}
    query = (
        select(AttendanceEvent)
        .where(
            AttendanceEvent.employee_id.in_(list(work_dates)),
            AttendanceEvent.work_date >= min(work_dates.values()),
            AttendanceEvent.work_date <= max(work_dates.values()),
        )
        .order_by(AttendanceEvent.occurred_at, AttendanceEvent.id)
    )
    found: dict[uuid.UUID, list[AttendanceEvent]] = defaultdict(list)
    for event in await session.scalars(query):
        if event.work_date == work_dates[event.employee_id]:
            found[event.employee_id].append(event)
    return found
