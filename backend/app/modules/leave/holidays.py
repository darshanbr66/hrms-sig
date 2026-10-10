"""Holiday calendars (docs/product-requirements.md LV-7, authorization-model.md §3.4).

- Reading calendars needs `holiday.read`; creating and changing them needs `holiday.manage`,
  and every change is audited (holiday calendars hold no personal data).
- Each location uses one calendar per year. An employee's holidays are those of the calendar
  their current location uses in each year of the range asked for.
- Nothing is seeded: which days are holidays is Sigvitas's decision, entered by HR.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leave.models import Holiday, HolidayCalendar, LocationHolidayCalendar
from app.modules.org import public as org
from app.modules.people import public as people
from app.platform.audit.records import AuditEvent, RequestContext, ValueChange, change_of
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer, Granted
from app.platform.clock import Clock
from app.platform.concurrency import check_version
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType

MAX_RANGE_DAYS: Final = 366


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


@dataclass(frozen=True, slots=True)
class CalendarSummary:
    calendar: HolidayCalendar
    holiday_count: int
    location_ids: list[uuid.UUID]


@dataclass(frozen=True, slots=True)
class CalendarDetail:
    calendar: HolidayCalendar
    holidays: list[Holiday]
    locations: dict[uuid.UUID, str]


@dataclass(frozen=True, slots=True)
class EmployeeHoliday:
    holiday: Holiday
    calendar_name: str


class HolidayService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    # --- reads ---------------------------------------------------------------------------------

    async def calendars(self, year: int | None) -> list[CalendarSummary]:
        async with self._db.unit_of_work() as session:
            query = select(HolidayCalendar).order_by(
                HolidayCalendar.year.desc(), func.lower(HolidayCalendar.name)
            )
            if year is not None:
                query = query.where(HolidayCalendar.year == year)
            calendars = list(await session.scalars(query))
            ids = [calendar.id for calendar in calendars]
            counts = dict(
                (
                    await session.execute(
                        select(Holiday.calendar_id, func.count())
                        .where(Holiday.calendar_id.in_(ids))
                        .group_by(Holiday.calendar_id)
                    )
                ).all()
            )
            locations: dict[uuid.UUID, list[uuid.UUID]] = {}
            for row in await session.scalars(
                select(LocationHolidayCalendar).where(LocationHolidayCalendar.calendar_id.in_(ids))
            ):
                locations.setdefault(row.calendar_id, []).append(row.location_id)
        return [CalendarSummary(c, counts.get(c.id, 0), locations.get(c.id, [])) for c in calendars]

    async def calendar(self, calendar_id: uuid.UUID) -> CalendarDetail:
        async with self._db.unit_of_work() as session:
            return await self._detail(session, calendar_id)

    async def _detail(self, session: AsyncSession, calendar_id: uuid.UUID) -> CalendarDetail:
        calendar = await session.get(HolidayCalendar, calendar_id, populate_existing=True)
        if calendar is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        holidays = list(
            await session.scalars(
                select(Holiday).where(Holiday.calendar_id == calendar_id).order_by(Holiday.date)
            )
        )
        location_ids = list(
            await session.scalars(
                select(LocationHolidayCalendar.location_id).where(
                    LocationHolidayCalendar.calendar_id == calendar_id
                )
            )
        )
        names = await org.unit_names(session, location_ids=location_ids)
        return CalendarDetail(calendar, holidays, names.locations)

    async def own_holidays(self, actor: Actor, start: date, end: date) -> list[EmployeeHoliday]:
        """Holidays of the calendar the employee's current location uses, between two dates."""
        if end < start:
            raise _field("to", "range.reversed", "The end date must be on or after the start date.")
        if (end - start).days >= MAX_RANGE_DAYS:
            raise _field("to", "range.too_long", f"Choose a range of at most {MAX_RANGE_DAYS} days.")
        if actor.employee_id is None:
            raise ProblemError(
                ProblemType.NOT_FOUND, detail="Your account is not linked to an employee record."
            )
        async with self._db.unit_of_work() as session:
            await self._authz.require(session, actor, "holiday.read")
            placement = await people.placement(session, actor.employee_id, self._authz.today())
            if placement is None:
                return []
            rows = (
                await session.execute(
                    select(Holiday, HolidayCalendar.name)
                    .join(HolidayCalendar, HolidayCalendar.id == Holiday.calendar_id)
                    .join(
                        LocationHolidayCalendar,
                        LocationHolidayCalendar.calendar_id == HolidayCalendar.id,
                    )
                    .where(
                        LocationHolidayCalendar.location_id == placement.location_id,
                        HolidayCalendar.status == "active",
                        Holiday.date >= start,
                        Holiday.date <= end,
                    )
                    .order_by(Holiday.date)
                )
            ).all()
        return [EmployeeHoliday(row[0], row[1]) for row in rows]

    # --- changes -------------------------------------------------------------------------------

    async def create_calendar(
        self, actor: Actor, context: RequestContext, name: str, year: int
    ) -> CalendarDetail:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "holiday.manage")
            calendar = HolidayCalendar(name=name, year=year)
            session.add(calendar)
            await _flush(session)
            await self._record(
                session,
                actor,
                context,
                granted,
                action="holiday.calendar.created",
                calendar_id=calendar.id,
                changes={"name": ValueChange(None, name), "year": ValueChange(None, year)},
            )
            return await self._detail(session, calendar.id)

    async def update_calendar(
        self,
        actor: Actor,
        context: RequestContext,
        calendar_id: uuid.UUID,
        *,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> CalendarDetail:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "holiday.manage")
            calendar = await self._locked(session, calendar_id, expected_version)
            for required in ("name", "status"):
                if required in values and values[required] is None:
                    raise _field(required, "holiday.required", "Enter a value.")
            changes = {
                name: change_of(name, getattr(calendar, name), value)
                for name, value in values.items()
                if getattr(calendar, name) != value
            }
            if changes:
                for name, value in values.items():
                    setattr(calendar, name, value)
                await _flush(session)
                await self._record(
                    session,
                    actor,
                    context,
                    granted,
                    action="holiday.calendar.updated",
                    calendar_id=calendar.id,
                    changes=changes,
                )
            return await self._detail(session, calendar_id)

    async def add_holiday(
        self,
        actor: Actor,
        context: RequestContext,
        calendar_id: uuid.UUID,
        *,
        expected_version: int,
        holiday_date: date,
        name: str,
        is_optional: bool,
    ) -> CalendarDetail:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "holiday.manage")
            calendar = await self._locked(session, calendar_id, expected_version)
            if holiday_date.year != calendar.year:
                raise _field("date", "holiday.outside_year", f"Choose a date in {calendar.year}.")
            holiday = Holiday(calendar_id=calendar_id, date=holiday_date, name=name, is_optional=is_optional)
            session.add(holiday)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise _field(
                    "date", "holiday.date_taken", "This calendar already has a holiday on that date."
                ) from exc
            await self._record(
                session,
                actor,
                context,
                granted,
                action="holiday.added",
                calendar_id=calendar.id,
                changes={
                    "date": ValueChange(None, holiday_date),
                    "name": ValueChange(None, name),
                    "is_optional": ValueChange(None, is_optional),
                },
            )
            return await self._detail(session, calendar_id)

    async def remove_holiday(
        self,
        actor: Actor,
        context: RequestContext,
        calendar_id: uuid.UUID,
        holiday_id: uuid.UUID,
        *,
        expected_version: int,
    ) -> CalendarDetail:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "holiday.manage")
            await self._locked(session, calendar_id, expected_version)
            holiday = await session.get(Holiday, holiday_id)
            if holiday is None or holiday.calendar_id != calendar_id:
                raise ProblemError(ProblemType.NOT_FOUND)
            await session.delete(holiday)
            await session.flush()
            await self._record(
                session,
                actor,
                context,
                granted,
                action="holiday.removed",
                calendar_id=calendar_id,
                changes={"date": ValueChange(holiday.date, None), "name": ValueChange(holiday.name, None)},
            )
            return await self._detail(session, calendar_id)

    async def assign_location(
        self,
        actor: Actor,
        context: RequestContext,
        calendar_id: uuid.UUID,
        location_id: uuid.UUID,
        *,
        expected_version: int,
    ) -> CalendarDetail:
        """The location uses this calendar for the calendar's year, replacing any other."""
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "holiday.manage")
            calendar = await self._locked(session, calendar_id, expected_version)
            if not (await org.unit_names(session, location_ids=[location_id])).locations:
                raise _field("location_id", "holiday.unknown_location", "Choose an existing location.")
            current = (
                await session.scalars(
                    select(LocationHolidayCalendar)
                    .where(
                        LocationHolidayCalendar.location_id == location_id,
                        LocationHolidayCalendar.year == calendar.year,
                    )
                    .with_for_update()
                )
            ).first()
            if current is not None and current.calendar_id == calendar_id:
                return await self._detail(session, calendar_id)
            previous = current.calendar_id if current else None
            if current is not None:
                await session.delete(current)
                await session.flush()
            session.add(
                LocationHolidayCalendar(location_id=location_id, calendar_id=calendar_id, year=calendar.year)
            )
            await session.flush()
            await self._record(
                session,
                actor,
                context,
                granted,
                action="holiday.calendar.location_assigned",
                calendar_id=calendar_id,
                changes={
                    "location_id": ValueChange(None, location_id),
                    "replaced_calendar_id": ValueChange(previous, None),
                },
            )
            return await self._detail(session, calendar_id)

    async def unassign_location(
        self,
        actor: Actor,
        context: RequestContext,
        calendar_id: uuid.UUID,
        location_id: uuid.UUID,
        *,
        expected_version: int,
    ) -> CalendarDetail:
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, "holiday.manage")
            calendar = await self._locked(session, calendar_id, expected_version)
            current = (
                await session.scalars(
                    select(LocationHolidayCalendar).where(
                        LocationHolidayCalendar.location_id == location_id,
                        LocationHolidayCalendar.calendar_id == calendar.id,
                    )
                )
            ).first()
            if current is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            await session.delete(current)
            await session.flush()
            await self._record(
                session,
                actor,
                context,
                granted,
                action="holiday.calendar.location_unassigned",
                calendar_id=calendar_id,
                changes={"location_id": ValueChange(location_id, None)},
            )
            return await self._detail(session, calendar_id)

    # --- helpers -------------------------------------------------------------------------------

    async def _locked(
        self, session: AsyncSession, calendar_id: uuid.UUID, expected_version: int
    ) -> HolidayCalendar:
        """The calendar, locked and checked against the version the change was based on. Every
        change to the calendar or its holidays and locations moves its version on."""
        calendar = await session.get(
            HolidayCalendar, calendar_id, with_for_update=True, populate_existing=True
        )
        if calendar is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        check_version(calendar.version, expected_version)
        calendar.version += 1
        calendar.updated_at = self._clock.now()
        return calendar

    async def _record(
        self,
        session: AsyncSession,
        actor: Actor,
        context: RequestContext,
        granted: Granted,
        *,
        action: str,
        calendar_id: uuid.UUID,
        changes: Mapping[str, Any],
    ) -> None:
        await self._audit.record(
            session,
            AuditEvent(
                action=action,
                actor=actor.audit_actor(),
                context=context,
                permission_used=granted.key,
                target_type="holiday_calendar",
                target_id=calendar_id,
                changes=changes,
            ),
        )


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        if "uq_holiday_calendars_name_year" in str(exc.orig):
            raise _conflict(
                "holiday.calendar_exists", "A calendar with this name already exists for that year."
            ) from exc
        raise
