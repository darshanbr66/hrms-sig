"""HTTP routes for attendance (docs/api-architecture.md §8, Attendance)."""

import uuid
from datetime import date
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, Query, Request

from app.modules.attendance import schemas as s
from app.modules.attendance.service import AttendanceService, Day, TeamMember, Today
from app.modules.attendance.timeline import EventType
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.pagination import decode_key_cursor, encode_key_cursor

router = APIRouter(prefix="/api/v1", tags=["attendance"])

MAX_LIMIT: Final = 100
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=100, pattern=r"^[\x21-\x7e]+$")
]
From = Annotated[date, Query(alias="from")]
To = Annotated[date, Query(alias="to")]


def service(request: Request) -> AttendanceService:
    value: AttendanceService = request.app.state.attendance_service
    return value


Service = Annotated[AttendanceService, Depends(service)]


def _day(day: Day) -> s.DayView:
    totals = day.totals
    return s.DayView(
        work_date=day.work_date,
        first_in=totals.first_in,
        last_out=totals.last_out,
        session_seconds=totals.session_seconds,
        break_seconds=totals.break_seconds,
        open_session=totals.open_session,
        on_break=totals.on_break,
        missing_break_end=totals.missing_break_end,
        events=[
            s.EventView(
                id=event.id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                source=event.source,
            )
            for event in day.events
        ],
    )


def _today(today: Today) -> s.TodayView:
    order = list(EventType)
    return s.TodayView(
        state=today.state.value,
        work_date=today.work_date,
        time_zone=today.time_zone,
        server_time=today.server_time,
        allowed_events=[event.value for event in sorted(today.allowed, key=order.index)],
        open_since=today.open_since,
        day=_day(today.day),
    )


def _member(member: TeamMember) -> s.TeamMemberToday:
    colleague = member.colleague
    return s.TeamMemberToday(
        employee=s.TeamMemberRef(
            id=colleague.id,
            name=colleague.name,
            employee_code=colleague.employee_code,
            department=colleague.department_name,
            designation=colleague.designation_name,
            location=colleague.location_name,
        ),
        state=member.state.value,
        work_date=member.work_date,
        first_in=member.totals.first_in,
        last_event_at=member.last_event_at,
        session_seconds=member.totals.session_seconds,
        break_seconds=member.totals.break_seconds,
    )


@router.get("/me/attendance/today", response_model=s.TodayView)
async def my_attendance_today(
    attendance: Service, actor: Annotated[Actor, requires("attendance.read.self")]
) -> s.TodayView:
    return _today(await attendance.today(actor))


@router.post("/me/attendance/events", response_model=s.TodayView)
async def record_attendance_event(
    body: s.EventCreate,
    request: Request,
    attendance: Service,
    actor: Annotated[Actor, requires("attendance.clock.self")],
    idempotency_key: IdempotencyKey,
) -> s.TodayView:
    """Records the event at the server's time and returns the updated day. Requires an
    `Idempotency-Key`: repeating a request with the same key does not record it twice."""
    today = await attendance.record(
        actor,
        RequestContext.from_request(request),
        event_type=EventType(body.event_type),
        idempotency_key=idempotency_key,
        client_reported_at=body.client_reported_at,
    )
    return _today(today)


@router.get("/me/attendance/days", response_model=s.DayList)
async def my_attendance_days(
    attendance: Service,
    actor: Annotated[Actor, requires("attendance.read.self")],
    start: From,
    end: To,
) -> s.DayList:
    days, zone = await attendance.days(actor, None, start, end)
    return s.DayList(time_zone=zone, items=[_day(day) for day in days])


@router.get("/employees/{employee_id}/attendance/days", response_model=s.DayList)
async def employee_attendance_days(
    employee_id: uuid.UUID,
    attendance: Service,
    actor: Annotated[Actor, requires("attendance.read")],
    start: From,
    end: To,
) -> s.DayList:
    days, zone = await attendance.days(actor, employee_id, start, end)
    return s.DayList(time_zone=zone, items=[_day(day) for day in days])


@router.get("/attendance/today", response_model=s.TeamTodayView)
async def attendance_today(
    *,
    attendance: Service,
    actor: Annotated[Actor, requires("attendance.read")],
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: Annotated[str | None, Query(max_length=400)] = None,
) -> s.TeamTodayView:
    """Where everyone in the viewer's scope stands today, and counts over the whole scope."""
    team = await attendance.team_today(
        actor,
        text_query=q,
        department_id=department_id,
        location_id=location_id,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return s.TeamTodayView(
        summary=s.TeamSummaryView(
            in_scope=team.summary.in_scope,
            clocked_in_today=team.summary.clocked_in_today,
            working_now=team.summary.working_now,
            on_break=team.summary.on_break,
        ),
        items=[_member(member) for member in team.members],
        next_cursor=encode_key_cursor(team.next_after),
    )
