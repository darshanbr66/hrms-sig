"""HTTP routes for attendance (docs/api-architecture.md §8, Attendance): clocking, days, the
register and report, corrections, and shift and policy configuration."""

import uuid
from datetime import date
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel

from app.modules.attendance import schemas as s
from app.modules.attendance.config import (
    AssignmentRecord,
    ConfigService,
    DefaultRecord,
    PolicyRecord,
    ShiftRecord,
    VersionValues,
)
from app.modules.attendance.correction_rules import Add
from app.modules.attendance.corrections import (
    CorrectionDetail,
    CorrectionRecord,
    CorrectionService,
    Proposal,
    field_error,
)
from app.modules.attendance.days import JudgedDay
from app.modules.attendance.models import AttendanceLock
from app.modules.attendance.service import AttendanceService, PeriodTotals, TeamMember, Today
from app.modules.attendance.timeline import EventType
from app.modules.people import public as people
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.concurrency import IfMatch, etag, expected_version
from app.platform.pagination import decode_key_cursor, encode_key_cursor

router = APIRouter(prefix="/api/v1", tags=["attendance"])

MAX_LIMIT: Final = 100
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=100, pattern=r"^[\x21-\x7e]+$")
]
From = Annotated[date, Query(alias="from")]
To = Annotated[date, Query(alias="to")]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=400)]
TextQuery = Annotated[str | None, Query(min_length=1, max_length=100)]
type StatusFilter = Literal[
    "present",
    "half_day",
    "absent",
    "on_leave",
    "holiday",
    "weekly_off",
    "incomplete",
    "pending_correction",
    "not_judged",
]


def service(request: Request) -> AttendanceService:
    value: AttendanceService = request.app.state.attendance_service
    return value


def correction_service(request: Request) -> CorrectionService:
    value: CorrectionService = request.app.state.correction_service
    return value


def config_service(request: Request) -> ConfigService:
    value: ConfigService = request.app.state.attendance_config_service
    return value


Service = Annotated[AttendanceService, Depends(service)]
Corrections = Annotated[CorrectionService, Depends(correction_service)]
Config = Annotated[ConfigService, Depends(config_service)]


def _sent(body: BaseModel) -> dict[str, Any]:
    return {name: getattr(body, name) for name in body.model_fields_set}


def _day(day: JudgedDay) -> s.DayView:
    totals, summary = day.totals, day.summary
    return s.DayView(
        work_date=day.work_date,
        first_in=totals.first_in,
        last_out=totals.last_out,
        session_seconds=totals.session_seconds,
        break_seconds=totals.break_seconds,
        open_session=totals.open_session,
        on_break=totals.on_break,
        missing_break_end=totals.missing_break_end,
        status=summary.status.value if summary.status else None,
        judged=summary.configured,
        shift_name=day.shift_name,
        shift_start_at=summary.shift_start_at,
        shift_end_at=summary.shift_end_at,
        effective_minutes=summary.effective_minutes,
        late_minutes=summary.late_minutes,
        early_leave_minutes=summary.early_leave_minutes,
        overtime_minutes=summary.overtime_minutes,
        anomalies=list(summary.anomalies),
        holiday=day.holiday,
        leave_portion=str(day.leave_portion.normalize()),
        events=[
            s.EventView(
                id=item.event.id,
                event_type=item.event.event_type,
                occurred_at=item.event.occurred_at,
                source=item.event.source,
                voided=item.voided,
                correction_id=item.event.correction_id,
            )
            for item in day.events
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


def _ref(colleague: people.Colleague) -> s.TeamMemberRef:
    return s.TeamMemberRef(
        id=colleague.id,
        name=colleague.name,
        employee_code=colleague.employee_code,
        department=colleague.department_name,
        designation=colleague.designation_name,
        location=colleague.location_name,
    )


def _member(member: TeamMember) -> s.TeamMemberToday:
    return s.TeamMemberToday(
        employee=_ref(member.colleague),
        state=member.state.value,
        work_date=member.work_date,
        first_in=member.totals.first_in,
        last_event_at=member.last_event_at,
        session_seconds=member.totals.session_seconds,
        break_seconds=member.totals.break_seconds,
        time_zone=member.time_zone,
        open_since=member.open_since,
    )


def _report_row(row: PeriodTotals) -> s.AttendanceReportRow:
    return s.AttendanceReportRow(
        employee=_ref(row.colleague),
        days=dict(row.days),
        not_judged_days=row.not_judged,
        effective_minutes=row.effective_minutes,
        late_days=row.late_days,
        late_minutes=row.late_minutes,
        early_leave_days=row.early_leave_days,
        overtime_minutes=row.overtime_minutes,
    )


# --- clocking and own days --------------------------------------------------------------------


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


# --- team, register, report -------------------------------------------------------------------


@router.get("/attendance/today", response_model=s.TeamTodayView)
async def attendance_today(
    *,
    attendance: Service,
    actor: Annotated[Actor, requires("attendance.read")],
    q: TextQuery = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    limit: Limit = 50,
    cursor: Cursor = None,
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


@router.get("/attendance/register", response_model=s.RegisterView)
async def attendance_register(
    *,
    attendance: Service,
    actor: Annotated[Actor, requires("attendance.read")],
    on: Annotated[date, Query(alias="date")],
    q: TextQuery = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    day_status: Annotated[StatusFilter | None, Query(alias="status")] = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> s.RegisterView:
    """Everyone in the viewer's scope employed on the date, with that day judged."""
    register = await attendance.register(
        actor,
        on=on,
        text_query=q,
        department_id=department_id,
        location_id=location_id,
        status=day_status,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return s.RegisterView(
        date=register.on,
        in_scope=register.in_scope,
        counts=register.counts,
        items=[
            s.RegisterEntryView(employee=_ref(e.colleague), time_zone=e.day.time_zone, day=_day(e.day))
            for e in register.entries
        ],
        next_cursor=encode_key_cursor(register.next_after),
    )


@router.get("/attendance/report", response_model=s.AttendanceReportView)
async def attendance_report(
    *,
    attendance: Service,
    actor: Annotated[Actor, requires("attendance.read")],
    start: From,
    end: To,
    q: TextQuery = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> s.AttendanceReportView:
    """Per person in scope: days per status and the period's totals (RPT-1: only what the
    viewer may already read)."""
    report = await attendance.report(
        actor,
        start=start,
        end=end,
        text_query=q,
        department_id=department_id,
        location_id=location_id,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return s.AttendanceReportView(
        start=report.start,
        end=report.end,
        items=[_report_row(row) for row in report.rows],
        next_cursor=encode_key_cursor(report.next_after),
    )


# --- corrections ------------------------------------------------------------------------------


def _proposal(body: s.CorrectionCreate) -> Proposal:
    adds: list[Add] = []
    voids: list[uuid.UUID] = []
    for item in body.items:
        if item.action == "add":
            if item.event_type is None or item.proposed_at is None or item.target_event_id is not None:
                raise field_error("items", "correction.add_incomplete", "Give the event and its time.")
            adds.append(Add(EventType(item.event_type), item.proposed_at))
        else:
            if item.target_event_id is None or item.event_type is not None or item.proposed_at is not None:
                raise field_error("items", "correction.void_incomplete", "Choose the event to remove.")
            voids.append(item.target_event_id)
    return Proposal(body.work_date, body.reason, tuple(adds), tuple(voids))


def _correction_summary(record: CorrectionRecord) -> s.CorrectionSummary:
    c = record.correction
    return s.CorrectionSummary(
        id=c.id,
        employee_id=c.employee_id,
        employee_name=record.employee_name,
        work_date=c.work_date,
        kind=c.kind,
        status=c.status,
        requested_at=c.requested_at,
        decided_at=c.decided_at,
        locked_period=c.locked_period,
    )


def _correction(detail: CorrectionDetail, response: Response | None = None) -> s.CorrectionDetailView:
    c = detail.record.correction
    if response is not None:
        response.headers["ETag"] = etag(c.version)
    return s.CorrectionDetailView(
        correction=_correction_summary(detail.record),
        reason=c.reason if detail.reason_visible else None,
        decision_note=c.decision_note if detail.reason_visible else None,
        requested_by=detail.requested_by,
        decided_by=detail.decided_by,
        items=[
            s.CorrectionItemView(
                action=item.action,
                event_type=item.event_type,
                proposed_at=item.proposed_at,
                target_event_id=item.target_event_id,
            )
            for item in detail.items
        ],
        time_zone=detail.time_zone,
        before=[
            s.TimelineEventView(event_type=e.event_type, occurred_at=e.occurred_at) for e in detail.before
        ],
        after=[s.TimelineEventView(event_type=e.event_type, occurred_at=e.occurred_at) for e in detail.after],
        still_valid=detail.still_valid,
        can_decide=detail.can_decide,
        can_cancel=detail.can_cancel,
        version=c.version,
    )


@router.post(
    "/me/attendance/corrections", response_model=s.CorrectionDetailView, status_code=status.HTTP_201_CREATED
)
async def request_correction(
    body: s.CorrectionCreate,
    request: Request,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.correction.request.self")],
    idempotency_key: IdempotencyKey,
) -> s.CorrectionDetailView:
    """Ask to add or remove events on one of your work dates, with a reason."""
    detail = await corrections.submit(
        actor, RequestContext.from_request(request), _proposal(body), idempotency_key
    )
    return _correction(detail)


@router.get("/me/attendance/corrections", response_model=s.CorrectionList)
async def my_corrections(
    *,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.correction.request.self")],
    limit: Limit = 50,
    cursor: Cursor = None,
) -> s.CorrectionList:
    listing = await corrections.corrections(
        actor,
        own=True,
        for_decision=False,
        statuses=(),
        employee_id=None,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return s.CorrectionList(
        items=[_correction_summary(r) for r in listing.records],
        next_cursor=encode_key_cursor(listing.next_after),
    )


@router.post("/me/attendance/corrections/{correction_id}/cancel", response_model=s.CorrectionDetailView)
async def cancel_correction(
    correction_id: uuid.UUID,
    request: Request,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.correction.request.self")],
) -> s.CorrectionDetailView:
    return _correction(await corrections.cancel(actor, RequestContext.from_request(request), correction_id))


@router.post(
    "/employees/{employee_id}/attendance/corrections",
    response_model=s.CorrectionDetailView,
    status_code=status.HTTP_201_CREATED,
)
async def record_correction(
    *,
    employee_id: uuid.UUID,
    body: s.CorrectionCreate,
    request: Request,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.correction.create.all")],
    idempotency_key: IdempotencyKey,
) -> s.CorrectionDetailView:
    """HR records a correction on the employee's behalf (ATT-8). It applies at once and the
    employee and their manager are told (SOD-7). Never on one's own attendance."""
    detail = await corrections.record_for(
        actor, RequestContext.from_request(request), employee_id, _proposal(body), idempotency_key
    )
    return _correction(detail)


@router.get("/attendance/corrections", response_model=s.CorrectionList)
async def list_corrections(
    *,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.read")],
    for_decision: bool = False,
    correction_status: Annotated[
        list[Literal["pending", "approved", "rejected", "cancelled"]] | None, Query(alias="status")
    ] = None,
    employee_id: uuid.UUID | None = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> s.CorrectionList:
    """Corrections in the viewer's scope, or with `for_decision` those waiting for a decision
    the viewer may make. Reasons are never listed."""
    listing = await corrections.corrections(
        actor,
        own=False,
        for_decision=for_decision,
        statuses=tuple(correction_status or ()),
        employee_id=employee_id,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return s.CorrectionList(
        items=[_correction_summary(r) for r in listing.records],
        next_cursor=encode_key_cursor(listing.next_after),
    )


@router.get("/attendance/corrections/{correction_id}", response_model=s.CorrectionDetailView)
async def correction_detail(
    correction_id: uuid.UUID,
    request: Request,
    response: Response,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.read")],
) -> s.CorrectionDetailView:
    """The correction with the day before and after it, side by side (ATT-7)."""
    detail = await corrections.detail(actor, RequestContext.from_request(request), correction_id)
    return _correction(detail, response)


@router.post("/attendance/corrections/{correction_id}/approve", response_model=s.CorrectionDetailView)
async def approve_correction(
    correction_id: uuid.UUID,
    body: s.CorrectionDecision,
    request: Request,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.correction.approve")],
) -> s.CorrectionDetailView:
    detail = await corrections.decide(
        actor, RequestContext.from_request(request), correction_id, approve=True, note=body.note
    )
    return _correction(detail)


@router.post("/attendance/corrections/{correction_id}/reject", response_model=s.CorrectionDetailView)
async def reject_correction(
    correction_id: uuid.UUID,
    body: s.CorrectionDecision,
    request: Request,
    corrections: Corrections,
    actor: Annotated[Actor, requires("attendance.correction.approve")],
) -> s.CorrectionDetailView:
    detail = await corrections.decide(
        actor, RequestContext.from_request(request), correction_id, approve=False, note=body.note
    )
    return _correction(detail)


# --- shifts and policies ----------------------------------------------------------------------


def _version_values(body: s.ShiftVersionCreate) -> VersionValues:
    weekdays = [off.weekday for off in body.weekly_offs]
    if len(set(weekdays)) != len(weekdays):
        raise field_error("weekly_offs", "shift.weekday_twice", "List each weekday once.")
    return VersionValues(
        effective_from=body.effective_from,
        start_time=body.start_time.replace(second=0, microsecond=0, tzinfo=None),
        end_time=body.end_time.replace(second=0, microsecond=0, tzinfo=None),
        weekly_offs=tuple((off.weekday, tuple(off.weeks) if off.weeks else None) for off in body.weekly_offs),
    )


def _shift(record: ShiftRecord, response: Response | None = None) -> s.ShiftView:
    shift = record.shift
    if response is not None:
        response.headers["ETag"] = etag(shift.version)
    return s.ShiftView(
        id=shift.id,
        code=shift.code,
        name=shift.name,
        description=shift.description,
        status=shift.status,
        version=shift.version,
        versions=[
            s.ShiftVersionView(
                id=v.id,
                version_no=v.version_no,
                effective_from=v.effective_from,
                effective_to=v.effective_to,
                start_time=v.start_time,
                end_time=v.end_time,
                crosses_midnight=v.crosses_midnight,
                weekly_offs=[
                    s.WeeklyOffView(weekday=o["weekday"], weeks=o.get("weeks")) for o in v.weekly_offs
                ],
            )
            for v in record.versions
        ],
    )


@router.get("/shifts", response_model=s.ShiftList)
async def list_shifts(
    config: Config, _actor: Annotated[Actor, requires("shift.read")], include_archived: bool = False
) -> s.ShiftList:
    return s.ShiftList(items=[_shift(r) for r in await config.shifts(include_archived=include_archived)])


@router.post("/shifts", response_model=s.ShiftView, status_code=status.HTTP_201_CREATED)
async def create_shift(
    body: s.ShiftCreate,
    request: Request,
    response: Response,
    config: Config,
    actor: Annotated[Actor, requires("shift.manage")],
) -> s.ShiftView:
    values = {"code": body.code, "name": body.name, "description": body.description}
    record = await config.create_shift(
        actor, RequestContext.from_request(request), values, _version_values(body.version)
    )
    return _shift(record, response)


@router.patch("/shifts/{shift_id}", response_model=s.ShiftView)
async def update_shift(
    *,
    shift_id: uuid.UUID,
    body: s.ShiftUpdate,
    request: Request,
    response: Response,
    config: Config,
    actor: Annotated[Actor, requires("shift.manage")],
    if_match: IfMatch = None,
) -> s.ShiftView:
    values = _sent(body)
    for name in ("name", "status"):
        if name in values and values[name] is None:
            raise field_error(name, "shift.required", "Choose a value.")
    record = await config.update_shift(
        actor,
        RequestContext.from_request(request),
        shift_id,
        expected_version=expected_version(if_match),
        values=values,
    )
    return _shift(record, response)


@router.post("/shifts/{shift_id}/versions", response_model=s.ShiftView, status_code=status.HTTP_201_CREATED)
async def add_shift_version(
    *,
    shift_id: uuid.UUID,
    body: s.ShiftVersionCreate,
    request: Request,
    response: Response,
    config: Config,
    actor: Annotated[Actor, requires("shift.manage")],
) -> s.ShiftView:
    """New times or weekly offs from a date. The current version ends that day; past days keep
    the version they were judged with."""
    record = await config.add_version(
        actor, RequestContext.from_request(request), shift_id, _version_values(body)
    )
    return _shift(record, response)


def _default(record: DefaultRecord) -> s.DefaultShiftView:
    d = record.default
    return s.DefaultShiftView(
        id=d.id,
        location_id=d.location_id,
        location_name=record.location_name,
        shift_id=d.shift_id,
        shift_name=record.shift_name,
        effective_from=d.effective_from,
        effective_to=d.effective_to,
    )


@router.get("/location-default-shifts", response_model=s.DefaultShiftList)
async def list_default_shifts(
    config: Config, _actor: Annotated[Actor, requires("shift.read")]
) -> s.DefaultShiftList:
    return s.DefaultShiftList(items=[_default(r) for r in await config.defaults()])


@router.post(
    "/location-default-shifts", response_model=s.DefaultShiftView, status_code=status.HTTP_201_CREATED
)
async def set_default_shift(
    body: s.DefaultShiftCreate,
    request: Request,
    config: Config,
    actor: Annotated[Actor, requires("shift.manage")],
) -> s.DefaultShiftView:
    """The shift everyone at a location works unless they have their own assignment."""
    record = await config.set_default(
        actor, RequestContext.from_request(request), body.location_id, body.shift_id, body.effective_from
    )
    return _default(record)


def _assignment(record: AssignmentRecord) -> s.ShiftAssignmentView:
    a = record.assignment
    return s.ShiftAssignmentView(
        id=a.id,
        shift_id=a.shift_id,
        shift_name=record.shift_name,
        effective_from=a.effective_from,
        effective_to=a.effective_to,
    )


@router.get("/employees/{employee_id}/shift-assignments", response_model=s.ShiftAssignmentList)
async def employee_shift_assignments(
    employee_id: uuid.UUID, config: Config, actor: Annotated[Actor, requires("attendance.read")]
) -> s.ShiftAssignmentList:
    return s.ShiftAssignmentList(items=[_assignment(r) for r in await config.assignments(actor, employee_id)])


@router.post("/shift-assignments", response_model=s.ShiftAssignmentResult)
async def assign_shift(
    body: s.ShiftAssignmentCreate,
    request: Request,
    config: Config,
    actor: Annotated[Actor, requires("shift.assign.all")],
) -> s.ShiftAssignmentResult:
    """Assign a shift from a date to one or more people; each one's current assignment ends
    that day."""
    result = await config.assign(
        actor, RequestContext.from_request(request), body.employee_ids, body.shift_id, body.effective_from
    )
    return s.ShiftAssignmentResult(
        assigned=result.assigned, skipped_not_permitted=result.skipped_not_permitted
    )


def _policy(record: PolicyRecord) -> s.PolicyView:
    p = record.policy
    return s.PolicyView(
        id=p.id,
        scope_type=p.scope_type,
        scope_id=p.scope_id,
        scope_name=record.scope_name,
        effective_from=p.effective_from,
        effective_to=p.effective_to,
        grace_minutes=p.grace_minutes,
        early_departure_grace_minutes=p.early_departure_grace_minutes,
        duration_basis=p.duration_basis,
        full_day_min_minutes=p.full_day_min_minutes,
        half_day_min_minutes=p.half_day_min_minutes,
        max_break_minutes=p.max_break_minutes,
        breaks_count_as_work=p.breaks_count_as_work,
        break_excess_action=p.break_excess_action,
        overtime_enabled=p.overtime_enabled,
        overtime_basis=p.overtime_basis,
        overtime_min_minutes=p.overtime_min_minutes,
        overtime_requires_approval=p.overtime_requires_approval,
        incomplete_after_minutes=p.incomplete_after_minutes,
        allow_multiple_sessions=p.allow_multiple_sessions,
    )


@router.get("/attendance-policies", response_model=s.PolicyList)
async def list_attendance_policies(
    config: Config, _actor: Annotated[Actor, requires("shift.read")]
) -> s.PolicyList:
    return s.PolicyList(items=[_policy(r) for r in await config.policies()])


@router.post("/attendance-policies", response_model=s.PolicyView, status_code=status.HTTP_201_CREATED)
async def create_attendance_policy(
    body: s.PolicyCreate,
    request: Request,
    config: Config,
    actor: Annotated[Actor, requires("attendance.policy.manage")],
) -> s.PolicyView:
    """A new policy version for a scope from a date; the current one of that scope ends then.
    Every value is entered here: none is assumed."""
    return _policy(await config.create_policy(actor, RequestContext.from_request(request), body.model_dump()))


def _lock(lock: AttendanceLock) -> s.LockView:
    return s.LockView(
        id=lock.id,
        period_start=lock.period_start,
        period_end=lock.period_end,
        reason=lock.reason,
        locked_at=lock.locked_at,
        released_at=lock.released_at,
        release_reason=lock.release_reason,
    )


@router.get("/attendance/locks", response_model=s.LockList)
async def list_attendance_locks(
    config: Config, actor: Annotated[Actor, requires("attendance.read")]
) -> s.LockList:
    return s.LockList(items=[_lock(lock) for lock in await config.locks(actor)])


@router.post("/attendance/locks", response_model=s.LockView, status_code=status.HTTP_201_CREATED)
async def create_attendance_lock(
    body: s.LockCreate,
    request: Request,
    config: Config,
    actor: Annotated[Actor, requires("attendance.policy.manage")],
) -> s.LockView:
    """Close past dates to corrections, except by HR holding `attendance.correction.approve.all`."""
    lock = await config.create_lock(
        actor, RequestContext.from_request(request), body.period_start, body.period_end, body.reason
    )
    return _lock(lock)


@router.post("/attendance/locks/{lock_id}/release", response_model=s.LockView)
async def release_attendance_lock(
    lock_id: uuid.UUID,
    body: s.LockRelease,
    request: Request,
    config: Config,
    actor: Annotated[Actor, requires("attendance.policy.manage")],
) -> s.LockView:
    return _lock(await config.release_lock(actor, RequestContext.from_request(request), lock_id, body.reason))


@router.get("/attendance/setup", response_model=s.SetupView)
async def attendance_setup(
    config: Config, actor: Annotated[Actor, requires("attendance.read")]
) -> s.SetupView:
    """Who in the viewer's scope has no shift, or no attendance policy, today (ATT-12)."""
    gaps = await config.setup(actor)
    return s.SetupView(
        date=gaps.on,
        active_shifts=gaps.shifts,
        policies_in_effect=gaps.policies,
        organization_policy=gaps.organization_policy,
        employees_in_scope=gaps.employees_in_scope,
        without_shift=[_ref(c) for c in gaps.without_shift],
        without_policy=[_ref(c) for c in gaps.without_policy],
    )
