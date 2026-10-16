"""HTTP routes for holiday calendars and leave management (docs/api-architecture.md §8, Leave)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.leave import schemas as ls
from app.modules.leave.holidays import CalendarDetail, HolidayService
from app.modules.leave.models import HolidayKind, LeaveLedgerEntry, LeaveType
from app.modules.leave.service import (
    Balance,
    Draft,
    LeaveService,
    PolicyRecord,
    RequestDetail,
    RequestFilters,
    RequestListing,
    RequestRecord,
    vars_of,
)
from app.modules.leave.year import ImportRow, LeaveYearService
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.concurrency import IfMatch, etag, expected_version
from app.platform.pagination import decode_key_cursor, encode_key_cursor

router = APIRouter(prefix="/api/v1", tags=["holidays"])

type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
type Year = Annotated[int, Field(ge=2000, le=2100)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalendarCreate(_Request):
    name: Name
    year: Year


class CalendarUpdate(_Request):
    name: Name | None = None
    status: Literal["active", "archived"] | None = None


type HolidayKindValue = Literal["holiday", "working_day"]


class HolidayCreate(_Request):
    date: date
    name: Name
    # An optional (restricted) holiday employees may choose to take.
    is_optional: bool = False
    # `working_day`: worked although the shift has the date as a weekly off, for example a
    # Saturday announced as a working day. Never optional.
    kind: HolidayKindValue = "holiday"


class HolidayUpdate(_Request):
    """The name, or whether a holiday is optional. The date and the kind do not change."""

    name: Name | None = None
    is_optional: bool | None = None


def _kind(value: str) -> HolidayKindValue:
    return "working_day" if value == HolidayKind.WORKING_DAY else "holiday"


class HolidayView(_Response):
    id: uuid.UUID
    date: date
    name: str
    is_optional: bool
    kind: HolidayKindValue


class LocationRef(_Response):
    id: uuid.UUID
    name: str


class CalendarView(_Response):
    id: uuid.UUID
    name: str
    year: int
    status: Literal["active", "archived"]
    version: int
    holidays: list[HolidayView]
    locations: list[LocationRef]
    updated_at: datetime


class CalendarSummaryView(_Response):
    id: uuid.UUID
    name: str
    year: int
    status: Literal["active", "archived"]
    holiday_count: int
    working_day_count: int
    location_count: int


class CalendarList(_Response):
    items: list[CalendarSummaryView]


class EmployeeHolidayView(_Response):
    date: date
    name: str
    is_optional: bool
    kind: HolidayKindValue
    calendar: str


class EmployeeHolidayList(_Response):
    items: list[EmployeeHolidayView]


def service(request: Request) -> HolidayService:
    value: HolidayService = request.app.state.holiday_service
    return value


Service = Annotated[HolidayService, Depends(service)]


def _view(detail: CalendarDetail, response: Response) -> CalendarView:
    calendar = detail.calendar
    response.headers["ETag"] = etag(calendar.version)
    return CalendarView(
        id=calendar.id,
        name=calendar.name,
        year=calendar.year,
        status=calendar.status,
        version=calendar.version,
        holidays=[
            HolidayView(id=h.id, date=h.date, name=h.name, is_optional=h.is_optional, kind=_kind(h.kind))
            for h in detail.holidays
        ],
        locations=sorted(
            (LocationRef(id=i, name=n) for i, n in detail.locations.items()), key=lambda ref: ref.name.lower()
        ),
        updated_at=calendar.updated_at,
    )


def _sent(body: BaseModel) -> dict[str, Any]:
    return {name: getattr(body, name) for name in body.model_fields_set}


@router.get("/holiday-calendars", response_model=CalendarList)
async def list_calendars(
    holidays: Service,
    _actor: Annotated[Actor, requires("holiday.read")],
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> CalendarList:
    return CalendarList(
        items=[
            CalendarSummaryView(
                id=item.calendar.id,
                name=item.calendar.name,
                year=item.calendar.year,
                status=item.calendar.status,
                holiday_count=item.holiday_count,
                working_day_count=item.working_day_count,
                location_count=len(item.location_ids),
            )
            for item in await holidays.calendars(year)
        ]
    )


@router.post("/holiday-calendars", response_model=CalendarView, status_code=status.HTTP_201_CREATED)
async def create_calendar(
    body: CalendarCreate,
    request: Request,
    response: Response,
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.manage")],
) -> CalendarView:
    detail = await holidays.create_calendar(actor, RequestContext.from_request(request), body.name, body.year)
    return _view(detail, response)


@router.get("/holiday-calendars/{calendar_id}", response_model=CalendarView)
async def get_calendar(
    calendar_id: uuid.UUID,
    response: Response,
    holidays: Service,
    _actor: Annotated[Actor, requires("holiday.read")],
) -> CalendarView:
    return _view(await holidays.calendar(calendar_id), response)


@router.patch("/holiday-calendars/{calendar_id}", response_model=CalendarView)
async def update_calendar(
    *,
    calendar_id: uuid.UUID,
    body: CalendarUpdate,
    request: Request,
    response: Response,
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.manage")],
    if_match: IfMatch = None,
) -> CalendarView:
    detail = await holidays.update_calendar(
        actor,
        RequestContext.from_request(request),
        calendar_id,
        expected_version=expected_version(if_match),
        values=_sent(body),
    )
    return _view(detail, response)


@router.post("/holiday-calendars/{calendar_id}/holidays", response_model=CalendarView, status_code=201)
async def add_holiday(
    *,
    calendar_id: uuid.UUID,
    body: HolidayCreate,
    request: Request,
    response: Response,
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.manage")],
    if_match: IfMatch = None,
) -> CalendarView:
    detail = await holidays.add_holiday(
        actor,
        RequestContext.from_request(request),
        calendar_id,
        expected_version=expected_version(if_match),
        holiday_date=body.date,
        name=body.name,
        is_optional=body.is_optional,
        kind=HolidayKind(body.kind),
    )
    return _view(detail, response)


@router.patch("/holiday-calendars/{calendar_id}/holidays/{holiday_id}", response_model=CalendarView)
async def update_holiday(
    *,
    calendar_id: uuid.UUID,
    holiday_id: uuid.UUID,
    body: HolidayUpdate,
    request: Request,
    response: Response,
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.manage")],
    if_match: IfMatch = None,
) -> CalendarView:
    detail = await holidays.update_holiday(
        actor,
        RequestContext.from_request(request),
        calendar_id,
        holiday_id,
        expected_version=expected_version(if_match),
        values=_sent(body),
    )
    return _view(detail, response)


@router.delete("/holiday-calendars/{calendar_id}/holidays/{holiday_id}", response_model=CalendarView)
async def remove_holiday(
    *,
    calendar_id: uuid.UUID,
    holiday_id: uuid.UUID,
    request: Request,
    response: Response,
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.manage")],
    if_match: IfMatch = None,
) -> CalendarView:
    detail = await holidays.remove_holiday(
        actor,
        RequestContext.from_request(request),
        calendar_id,
        holiday_id,
        expected_version=expected_version(if_match),
    )
    return _view(detail, response)


@router.put("/holiday-calendars/{calendar_id}/locations/{location_id}", response_model=CalendarView)
async def assign_location(
    *,
    calendar_id: uuid.UUID,
    location_id: uuid.UUID,
    request: Request,
    response: Response,
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.manage")],
    if_match: IfMatch = None,
) -> CalendarView:
    """The location uses this calendar for its year, replacing the one it used before."""
    detail = await holidays.assign_location(
        actor,
        RequestContext.from_request(request),
        calendar_id,
        location_id,
        expected_version=expected_version(if_match),
    )
    return _view(detail, response)


@router.delete("/holiday-calendars/{calendar_id}/locations/{location_id}", response_model=CalendarView)
async def unassign_location(
    *,
    calendar_id: uuid.UUID,
    location_id: uuid.UUID,
    request: Request,
    response: Response,
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.manage")],
    if_match: IfMatch = None,
) -> CalendarView:
    detail = await holidays.unassign_location(
        actor,
        RequestContext.from_request(request),
        calendar_id,
        location_id,
        expected_version=expected_version(if_match),
    )
    return _view(detail, response)


@router.get("/me/holidays", response_model=EmployeeHolidayList)
async def my_holidays(
    holidays: Service,
    actor: Annotated[Actor, requires("holiday.read")],
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
) -> EmployeeHolidayList:
    """Holidays of the calendar your current location uses, between two dates."""
    items = await holidays.own_holidays(actor, start, end)
    return EmployeeHolidayList(
        items=[
            EmployeeHolidayView(
                date=item.holiday.date,
                name=item.holiday.name,
                is_optional=item.holiday.is_optional,
                kind=_kind(item.holiday.kind),
                calendar=item.calendar_name,
            )
            for item in items
        ]
    )


# --- leave management (docs/api-architecture.md §8, Leave) ----------------------------------------

leave_router = APIRouter(prefix="/api/v1", tags=["leave"])

MAX_LIMIT: Final = 100
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=100, pattern=r"^[\x21-\x7e]+$")
]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=400)]
Statuses = Annotated[list[ls.StatusValue] | None, Query(alias="status", max_length=5)]


def leave_service(request: Request) -> LeaveService:
    value: LeaveService = request.app.state.leave_service
    return value


Leave = Annotated[LeaveService, Depends(leave_service)]


def year_service(request: Request) -> LeaveYearService:
    value: LeaveYearService = request.app.state.leave_year_service
    return value


Years = Annotated[LeaveYearService, Depends(year_service)]


def _type_view(leave_type: LeaveType, response: Response | None = None) -> ls.LeaveTypeView:
    if response is not None:
        response.headers["ETag"] = etag(leave_type.version)
    return ls.LeaveTypeView.model_validate(leave_type, from_attributes=True)


def _policy_view(record: PolicyRecord) -> ls.LeavePolicyView:
    p = record.policy
    return ls.LeavePolicyView(
        id=p.id,
        leave_type=ls.Ref(id=p.leave_type_id, name=record.leave_type_name),
        location=ls.Ref(id=p.location_id, name=record.location_name or "") if p.location_id else None,
        employment_type=p.employment_type,
        effective_from=p.effective_from,
        effective_to=p.effective_to,
        leave_year_start_month=p.leave_year_start_month,
        leave_year_start_day=p.leave_year_start_day,
        accrual_method=p.accrual_method,
        accrual_amount=p.accrual_amount,
        accrual_frequency=p.accrual_frequency,
        accrual_timing=p.accrual_timing,
        prorate_on_joining=p.prorate_on_joining,
        prorate_on_exit=p.prorate_on_exit,
        proration_rounding=p.proration_rounding,
        allow_negative_balance=p.allow_negative_balance,
        max_negative_days=p.max_negative_days,
        carry_forward_mode=p.carry_forward_mode,
        carry_forward_cap=p.carry_forward_cap,
        excess_action=p.excess_action,
        carried_expire_after_months=p.carried_expire_after_months,
        created_at=p.created_at,
    )


def _balance_view(balance: Balance) -> ls.BalanceView:
    policy = balance.policy
    return ls.BalanceView(
        leave_type=ls.Ref(id=balance.leave_type.id, name=balance.leave_type.name),
        is_paid=balance.leave_type.is_paid,
        allows_half_day=balance.leave_type.allows_half_day,
        requires_approval=balance.leave_type.requires_approval,
        balance=balance.balance,
        reserved=balance.reserved,
        available=balance.available,
        eligible=policy is not None,
        allow_negative_balance=policy.allow_negative_balance if policy else False,
        max_negative_days=policy.max_negative_days if policy else None,
    )


def _ledger_view(entry: LeaveLedgerEntry, type_name: str) -> ls.LedgerEntryView:
    return ls.LedgerEntryView(
        id=entry.id,
        leave_type=ls.Ref(id=entry.leave_type_id, name=type_name),
        leave_year=entry.leave_year,
        entry_type=entry.entry_type,
        amount=entry.amount,
        effective_date=entry.effective_date,
        leave_request_id=entry.leave_request_id,
        period_start=entry.period_start,
        reason=entry.reason,
        created_at=entry.created_at,
    )


def _summary(record: RequestRecord) -> ls.RequestSummary:
    r = record.request
    return ls.RequestSummary(
        id=r.id,
        employee=ls.Ref(id=r.employee_id, name=record.employee_name),
        leave_type=ls.Ref(id=r.leave_type_id, name=record.leave_type_name),
        start_date=r.start_date,
        end_date=r.end_date,
        start_half=r.start_half,
        end_half=r.end_half,
        days_requested=r.days_requested,
        status=r.status,
        created_at=r.created_at,
    )


def _day_view(date_: date, portion: Decimal, counted: bool, reason: str | None) -> ls.LeaveDayView:
    return ls.LeaveDayView(date=date_, portion=portion, counted=counted, not_counted_reason=reason)


def _detail_view(detail: RequestDetail) -> ls.RequestDetail:
    r = detail.record.request
    return ls.RequestDetail(
        **_summary(detail.record).model_dump(),
        reason=r.reason if detail.reason_visible else None,
        reason_visible=detail.reason_visible,
        days=[_day_view(d.date, d.portion, d.counted, d.not_counted_reason) for d in detail.days],
        decisions=[
            ls.DecisionView(
                decision=d.decision.decision,
                decided_by=d.decided_by,
                note=d.decision.note if detail.reason_visible else None,
                decided_at=d.decision.decided_at,
            )
            for d in detail.decisions
        ],
        can_decide=detail.can_decide,
        can_cancel=detail.can_cancel,
        version=r.version,
    )


def _page(listing: RequestListing) -> ls.RequestPage:
    return ls.RequestPage(
        items=[_summary(r) for r in listing.records], next_cursor=encode_key_cursor(listing.next_after)
    )


def _draft(body: ls.RequestDraft) -> Draft:
    return Draft(body.leave_type_id, body.start_date, body.end_date, body.start_half, body.end_half)


# leave types


@leave_router.get("/leave-types", response_model=ls.LeaveTypeList)
async def list_leave_types(
    leave: Leave, actor: Annotated[Actor, requires("leave.read")], include_archived: bool = False
) -> ls.LeaveTypeList:
    return ls.LeaveTypeList(
        items=[_type_view(t) for t in await leave.leave_types(actor, include_archived=include_archived)]
    )


@leave_router.post("/leave-types", response_model=ls.LeaveTypeView, status_code=status.HTTP_201_CREATED)
async def create_leave_type(
    body: ls.LeaveTypeCreate,
    request: Request,
    response: Response,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.policy.manage")],
) -> ls.LeaveTypeView:
    created = await leave.create_type(actor, RequestContext.from_request(request), body.model_dump())
    return _type_view(created, response)


@leave_router.patch("/leave-types/{leave_type_id}", response_model=ls.LeaveTypeView)
async def update_leave_type(
    *,
    leave_type_id: uuid.UUID,
    body: ls.LeaveTypeUpdate,
    request: Request,
    response: Response,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.policy.manage")],
    if_match: IfMatch = None,
) -> ls.LeaveTypeView:
    updated = await leave.update_type(
        actor,
        RequestContext.from_request(request),
        leave_type_id,
        expected_version=expected_version(if_match),
        values=_sent(body),
    )
    return _type_view(updated, response)


# policies


@leave_router.get("/leave-policies", response_model=ls.LeavePolicyList)
async def list_leave_policies(
    leave: Leave, _actor: Annotated[Actor, requires("leave.read")], leave_type_id: uuid.UUID | None = None
) -> ls.LeavePolicyList:
    return ls.LeavePolicyList(items=[_policy_view(p) for p in await leave.policies(leave_type_id)])


@leave_router.post("/leave-policies", response_model=ls.LeavePolicyView, status_code=status.HTTP_201_CREATED)
async def create_leave_policy(
    body: ls.LeavePolicyCreate,
    request: Request,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.policy.manage")],
) -> ls.LeavePolicyView:
    """A new effective-dated version for a scope; the scope's open version ends the day it starts."""
    return _policy_view(
        await leave.create_policy(actor, RequestContext.from_request(request), body.model_dump())
    )


# balances and the ledger


@leave_router.get("/me/leave/balances", response_model=ls.BalanceList)
async def my_balances(leave: Leave, actor: Annotated[Actor, requires("leave.read")]) -> ls.BalanceList:
    return ls.BalanceList(items=[_balance_view(b) for b in await leave.balances(actor, None)])


@leave_router.get("/me/leave/ledger", response_model=ls.LedgerList)
async def my_ledger(
    leave: Leave, actor: Annotated[Actor, requires("leave.read")], leave_type_id: uuid.UUID | None = None
) -> ls.LedgerList:
    return ls.LedgerList(
        items=[_ledger_view(e, n) for e, n in await leave.ledger(actor, None, leave_type_id)]
    )


@leave_router.get("/employees/{employee_id}/leave/balances", response_model=ls.BalanceList)
async def employee_balances(
    employee_id: uuid.UUID, leave: Leave, actor: Annotated[Actor, requires("leave.read")]
) -> ls.BalanceList:
    return ls.BalanceList(items=[_balance_view(b) for b in await leave.balances(actor, employee_id)])


@leave_router.get("/employees/{employee_id}/leave/ledger", response_model=ls.LedgerList)
async def employee_ledger(
    employee_id: uuid.UUID,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.read")],
    leave_type_id: uuid.UUID | None = None,
) -> ls.LedgerList:
    return ls.LedgerList(
        items=[_ledger_view(e, n) for e, n in await leave.ledger(actor, employee_id, leave_type_id)]
    )


@leave_router.post(
    "/employees/{employee_id}/leave-adjustments",
    response_model=ls.LedgerEntryView,
    status_code=status.HTTP_201_CREATED,
)
async def adjust_balance(
    *,
    employee_id: uuid.UUID,
    body: ls.AdjustmentCreate,
    request: Request,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.balance.adjust")],
    idempotency_key: IdempotencyKey,
) -> ls.LedgerEntryView:
    """An adjustment or opening balance with a reason. Never on your own balance."""
    entry = await leave.adjust(
        actor, RequestContext.from_request(request), employee_id, body.model_dump(), idempotency_key
    )
    entries = await leave.ledger(actor, employee_id, entry.leave_type_id)
    name = next((n for e, n in entries if e.id == entry.id), "")
    return _ledger_view(entry, name)


@leave_router.post("/leave-allocations", response_model=ls.AllocationResult)
async def run_allocation(
    body: ls.AllocationRun,
    request: Request,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.balance.adjust")],
) -> ls.AllocationResult:
    """The upfront allocation of one leave year for every employee you may adjust; running it
    again allocates only to those not yet allocated for that year."""
    result = await leave.allocate(actor, RequestContext.from_request(request), body.leave_type_id, body.as_of)
    return ls.AllocationResult(**vars_of(result))


@leave_router.get("/leave/report", response_model=ls.LeaveReportView)
async def leave_report(
    *,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.read")],
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=400)] = None,
) -> ls.LeaveReportView:
    """Days taken and pending per person and leave type in the period, for the people the
    reader may see (RPT-1). Reasons are never included."""
    rows, next_after = await leave.report(
        actor,
        start=start,
        end=end,
        department_id=department_id,
        location_id=location_id,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return ls.LeaveReportView(
        start=start,
        end=end,
        items=[
            ls.LeaveReportRow(
                employee=ls.Ref(id=row.colleague.id, name=row.colleague.name),
                employee_code=row.colleague.employee_code,
                department=row.colleague.department_name,
                location=row.colleague.location_name,
                leave_type=ls.Ref(id=row.leave_type_id, name=row.leave_type_name),
                taken=row.taken,
                pending=row.pending,
            )
            for row in rows
        ],
        next_cursor=encode_key_cursor(next_after),
    )


@leave_router.post("/leave-year-end", response_model=ls.YearEndResult)
async def close_leave_year(
    body: ls.YearEndRun,
    request: Request,
    years: Years,
    actor: Annotated[Actor, requires("leave.balance.adjust")],
) -> ls.YearEndResult:
    """Close a leave year for one leave type: carry each balance forward up to the policy's
    cap and lapse or keep the rest. With `apply: false` nothing is recorded; each employee is
    closed at most once."""
    report = await years.close_year(
        actor, RequestContext.from_request(request), body.leave_type_id, body.year_end, apply=body.apply
    )
    return ls.YearEndResult(
        applied=report.applied,
        counts=report.counts,
        items=[
            ls.YearEndRowView(
                employee=ls.Ref(id=row.employee_id, name=row.employee_name),
                outcome=row.outcome,
                closing=row.closing,
                carried=row.carried,
                lapsed=row.lapsed,
                kept=row.kept,
            )
            for row in report.rows
        ],
    )


@leave_router.post("/leave-opening-balances", response_model=ls.ImportResult)
async def import_opening_balances(
    body: ls.OpeningBalanceImport,
    request: Request,
    years: Years,
    actor: Annotated[Actor, requires("leave.balance.adjust")],
) -> ls.ImportResult:
    """Check opening balances row by row; with `apply: true` and every row fine, record them
    as one import batch. A file with any refused row records nothing."""
    rows = [
        ImportRow(line, row.employee_code, row.leave_type_code, row.days, row.effective_date)
        for line, row in enumerate(body.rows, start=1)
    ]
    report = await years.import_opening_balances(
        actor, RequestContext.from_request(request), rows, body.reason, apply=body.apply
    )
    return ls.ImportResult(
        applied=report.applied,
        batch_id=report.batch_id,
        counts=report.counts,
        items=[
            ls.ImportRowView(
                line=row.line,
                employee_code=row.employee_code,
                leave_type_code=row.leave_type_code,
                outcome=row.outcome,
            )
            for row in report.rows
        ],
    )


# requests


@leave_router.post("/me/leave-requests/preview", response_model=ls.PreviewView)
async def preview_request(
    body: ls.RequestDraft, leave: Leave, actor: Annotated[Actor, requires("leave.request.self")]
) -> ls.PreviewView:
    """How the days count and what the balance would be, without saving anything."""
    preview = await leave.preview(actor, _draft(body))
    return ls.PreviewView(
        days=[_day_view(d.date, d.portion, d.counted, d.not_counted_reason) for d in preview.days],
        days_requested=preview.days_requested,
        available_before=preview.available_before,
        available_after=preview.available_before - preview.days_requested,
        within_balance=preview.within_balance,
        requires_approval=preview.requires_approval,
        weekly_offs_configured=False,
    )


@leave_router.post("/me/leave-requests", response_model=ls.RequestDetail, status_code=status.HTTP_201_CREATED)
async def submit_request(
    body: ls.RequestCreate,
    request: Request,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.request.self")],
    idempotency_key: IdempotencyKey,
) -> ls.RequestDetail:
    """Repeating a request with the same `Idempotency-Key` returns the first result."""
    detail = await leave.submit(
        actor, RequestContext.from_request(request), _draft(body), body.reason, idempotency_key
    )
    return _detail_view(detail)


@leave_router.get("/me/leave-requests", response_model=ls.RequestPage)
async def my_requests(
    leave: Leave,
    actor: Annotated[Actor, requires("leave.request.self")],
    statuses: Statuses = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> ls.RequestPage:
    listing = await leave.requests(
        actor,
        RequestFilters(statuses=tuple(statuses or ())),
        own=True,
        for_decision=False,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return _page(listing)


@leave_router.post("/me/leave-requests/{request_id}/cancel", response_model=ls.RequestDetail)
async def cancel_request(
    request_id: uuid.UUID,
    request: Request,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.request.self")],
) -> ls.RequestDetail:
    """Withdraw a pending request, or cancel approved leave that has not started (which goes to
    the approver when the leave type needs approval)."""
    return _detail_view(await leave.cancel(actor, RequestContext.from_request(request), request_id))


@leave_router.get("/leave-requests", response_model=ls.RequestPage)
async def list_requests(
    *,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.read")],
    statuses: Statuses = None,
    leave_type_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    awaiting_my_decision: bool = False,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> ls.RequestPage:
    """Requests in your scope, newest first. With `awaiting_my_decision`, the requests you may
    decide that are waiting for a decision, oldest first. No reasons."""
    filters = RequestFilters(
        statuses=tuple(statuses or ()),
        leave_type_id=leave_type_id,
        employee_id=employee_id,
        department_id=department_id,
        location_id=location_id,
        start=start,
        end=end,
    )
    listing = await leave.requests(
        actor,
        filters,
        own=False,
        for_decision=awaiting_my_decision,
        after=decode_key_cursor(cursor),
        limit=limit,
    )
    return _page(listing)


@leave_router.get("/leave-requests/{request_id}", response_model=ls.RequestDetail)
async def get_request(
    request_id: uuid.UUID, request: Request, leave: Leave, actor: Annotated[Actor, requires("leave.read")]
) -> ls.RequestDetail:
    """The reason is shown to the employee and to whoever may decide the request."""
    return _detail_view(await leave.detail(actor, RequestContext.from_request(request), request_id))


@leave_router.post("/leave-requests/{request_id}/approve", response_model=ls.RequestDetail)
async def approve_request(
    *,
    request_id: uuid.UUID,
    body: ls.Decide,
    request: Request,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.request.approve")],
    idempotency_key: IdempotencyKey,
) -> ls.RequestDetail:
    """Approve a pending request, or the cancellation asked for on approved leave."""
    detail = await leave.decide(
        actor,
        RequestContext.from_request(request),
        request_id,
        approve=True,
        note=body.note,
        idempotency_key=idempotency_key,
    )
    return _detail_view(detail)


@leave_router.post("/leave-requests/{request_id}/reject", response_model=ls.RequestDetail)
async def reject_request(
    *,
    request_id: uuid.UUID,
    body: ls.Decide,
    request: Request,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.request.approve")],
    idempotency_key: IdempotencyKey,
) -> ls.RequestDetail:
    """Reject a pending request, or keep approved leave whose cancellation was asked for. A note
    is required."""
    detail = await leave.decide(
        actor,
        RequestContext.from_request(request),
        request_id,
        approve=False,
        note=body.note,
        idempotency_key=idempotency_key,
    )
    return _detail_view(detail)


@leave_router.get("/leave/calendar", response_model=ls.LeaveCalendarView)
async def leave_calendar(
    *,
    leave: Leave,
    actor: Annotated[Actor, requires("leave.read")],
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
    leave_type_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
) -> ls.LeaveCalendarView:
    """Who is away in your scope (at most 93 days): leave type and dates, never the reason."""
    records, truncated = await leave.calendar(
        actor,
        start,
        end,
        RequestFilters(leave_type_id=leave_type_id, department_id=department_id, location_id=location_id),
    )
    return ls.LeaveCalendarView(
        items=[
            ls.LeaveCalendarEntry(
                request_id=r.request.id,
                employee=ls.Ref(id=r.request.employee_id, name=r.employee_name),
                leave_type=ls.Ref(id=r.request.leave_type_id, name=r.leave_type_name),
                start_date=r.request.start_date,
                end_date=r.request.end_date,
                start_half=r.request.start_half,
                end_half=r.request.end_half,
                status=r.request.status,
            )
            for r in records
        ],
        truncated=truncated,
    )
