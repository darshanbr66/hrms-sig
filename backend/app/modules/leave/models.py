"""SQLAlchemy models for the holiday tables of the `leave` schema (docs/database-design.md §4.6,
revision 0016). A trigger keeps holiday dates and location assignments inside the calendar's year."""

import uuid
from datetime import date, datetime

from sqlalchemy import ForeignKey, Index, SmallInteger, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "leave"


class HolidayCalendar(Base):
    __tablename__ = "holiday_calendars"
    __table_args__ = (UniqueConstraint("name", "year"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    name: Mapped[str]
    year: Mapped[int] = mapped_column(SmallInteger())
    status: Mapped[str] = mapped_column(server_default="active")
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("calendar_id", "date"), {"schema": SCHEMA})

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    calendar_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave.holiday_calendars.id", ondelete="RESTRICT")
    )
    date: Mapped[date]
    name: Mapped[str]
    is_optional: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LocationHolidayCalendar(Base):
    __tablename__ = "location_holiday_calendars"
    __table_args__ = (
        UniqueConstraint("location_id", "year"),
        Index("ix_location_holiday_calendars_calendar_id", "calendar_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("org.locations.id", ondelete="RESTRICT"))
    calendar_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave.holiday_calendars.id", ondelete="RESTRICT")
    )
    year: Mapped[int] = mapped_column(SmallInteger())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
