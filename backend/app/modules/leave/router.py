"""HTTP routes for holiday calendars (docs/api-architecture.md §8, Leave)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.leave.holidays import CalendarDetail, HolidayService
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.concurrency import IfMatch, etag, expected_version

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


class HolidayCreate(_Request):
    date: date
    name: Name
    # An optional (restricted) holiday employees may choose to take.
    is_optional: bool = False


class HolidayView(_Response):
    id: uuid.UUID
    date: date
    name: str
    is_optional: bool


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
    location_count: int


class CalendarList(_Response):
    items: list[CalendarSummaryView]


class EmployeeHolidayView(_Response):
    date: date
    name: str
    is_optional: bool
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
            HolidayView(id=h.id, date=h.date, name=h.name, is_optional=h.is_optional) for h in detail.holidays
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
                calendar=item.calendar_name,
            )
            for item in items
        ]
    )
