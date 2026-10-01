"""SQLAlchemy models for the core `people` tables (docs/database-design.md §4.4, revision 0004).

Length limits and allowed values are CHECK constraints in the migration; the enums below
mirror the allowed values. Job history rows are never deleted and only `effective_to` may
change (database trigger `people.guard_job_history`).
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import CITEXT, DATERANGE, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SCHEMA = "people"


class EmployeeStatus(StrEnum):
    PRE_JOINING = "pre_joining"
    ACTIVE = "active"
    ON_NOTICE = "on_notice"
    EXITED = "exited"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERN = "intern"


class JobChangeReason(StrEnum):
    JOINING = "joining"
    TRANSFER = "transfer"
    PROMOTION = "promotion"
    MANAGER_CHANGE = "manager_change"
    CORRECTION = "correction"
    OTHER = "other"


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = {"schema": SCHEMA}  # noqa: RUF012 (SQLAlchemy declarative attribute)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    employee_code: Mapped[str] = mapped_column(CITEXT(), unique=True)
    legal_first_name: Mapped[str]
    legal_last_name: Mapped[str | None]
    preferred_name: Mapped[str | None]
    work_email: Mapped[str | None] = mapped_column(CITEXT(), unique=True)
    work_phone: Mapped[str | None]
    date_of_joining: Mapped[date]
    date_of_exit: Mapped[date | None]
    status: Mapped[str]
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EmployeeJob(Base):
    __tablename__ = "employee_jobs"
    __table_args__ = (
        ExcludeConstraint(
            ("employee_id", "="),
            (func.daterange(text("effective_from"), text("effective_to"), type_=DATERANGE), "&&"),
            name="ex_employee_jobs_employee_id_effective",
            using="gist",
        ),
        Index(
            "ix_employee_jobs_manager_employee_id",
            "manager_employee_id",
            postgresql_where=text("manager_employee_id IS NOT NULL"),
        ),
        Index("ix_employee_jobs_department_id", "department_id"),
        Index("ix_employee_jobs_location_id", "location_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.employees.id", ondelete="RESTRICT"))
    effective_from: Mapped[date]
    # Exclusive end; NULL while the row is open-ended.
    effective_to: Mapped[date | None]
    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("org.departments.id", ondelete="RESTRICT"))
    designation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("org.designations.id", ondelete="RESTRICT"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("org.locations.id", ondelete="RESTRICT"))
    manager_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.employees.id", ondelete="RESTRICT")
    )
    employment_type: Mapped[str]
    change_reason: Mapped[str]
    notes: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
