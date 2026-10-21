"""People records (docs/database-design.md §4.4).

Allowed values, lengths and the rules within one document are part of each collection's
validator. Job history is never deleted and only `effective_to` (and `updated_at`) may change;
status history is append-only. Overlapping job rows of one person are refused by the service,
which writes them only while holding the employee record's lock (formerly an exclusion
constraint).

`Employee` keeps three derived fields current on every write: `sort_name` and `code_key`
(lower case, for sorting and keyset pages) and `search_text` (lower-cased names, code and work
email, for the directory's text search).
"""

import dataclasses
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import CASE_INSENSITIVE, Document, Index, Record, allowed, asc, utcnow


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


_EMAIL = {"maxLength": 254, "pattern": r"^[^@\s]+@[^@\s]+$"}
_NAME = {"minLength": 1, "maxLength": 100}


def display_name_of(preferred: str | None, first: str, last: str | None) -> str:
    """Preferred name (else legal first name), then legal last name."""
    return f"{preferred or first} {last or ''}".strip()


@dataclasses.dataclass(kw_only=True, eq=False)
class Employee(Record):
    __collection__ = "employees"
    __indexes__ = (
        Index(
            asc("organization_id", "employee_code"),
            name="uq_employees_employee_code",
            unique=True,
            collation=CASE_INSENSITIVE,
        ),
        Index(
            asc("organization_id", "work_email"),
            name="uq_employees_work_email",
            unique=True,
            collation=CASE_INSENSITIVE,
            partial={"work_email": {"$type": "string"}},
        ),
        Index(asc("organization_id", "status"), name="ix_employees_status"),
        Index(asc("organization_id", "sort_name", "_id"), name="ix_employees_sort_name"),
        Index(asc("organization_id", "code_key", "_id"), name="ix_employees_code_key"),
        Index(asc("organization_id", "date_of_joining", "_id"), name="ix_employees_date_of_joining"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "employee_code": {"minLength": 1, "maxLength": 32},
        "legal_first_name": _NAME,
        "legal_last_name": _NAME,
        "preferred_name": _NAME,
        "work_email": _EMAIL,
        "work_phone": {"minLength": 1, "maxLength": 32},
        "status": allowed(EmployeeStatus),
        "version": {"minimum": 1},
    }
    __expr_rules__ = (
        # The exit is on or after joining.
        {"$or": [{"$eq": ["$date_of_exit", None]}, {"$gte": ["$date_of_exit", "$date_of_joining"]}]},
        # An exited record has its last working day.
        {"$or": [{"$ne": ["$status", "exited"]}, {"$ne": ["$date_of_exit", None]}]},
    )

    employee_code: str
    legal_first_name: str
    legal_last_name: str | None = None
    preferred_name: str | None = None
    work_email: str | None = None
    work_phone: str | None = None
    date_of_joining: date
    date_of_exit: date | None = None
    status: str
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)
    # Derived (before_write).
    sort_name: str = ""
    code_key: str = ""
    search_text: str = ""

    @property
    def display_name(self) -> str:
        return display_name_of(self.preferred_name, self.legal_first_name, self.legal_last_name)

    def before_write(self) -> None:
        self.sort_name = self.display_name.lower()
        self.code_key = self.employee_code.lower()
        parts = (
            self.legal_first_name,
            self.legal_last_name or "",
            self.preferred_name or "",
            self.employee_code,
            self.work_email or "",
        )
        self.search_text = " ".join(parts).lower()


@dataclasses.dataclass(kw_only=True, eq=False)
class EmployeeJob(Record):
    __collection__ = "employee_jobs"
    # Job history is preserved: only the end of a row (and its update time) ever changes.
    __deletable__ = False
    __mutable__ = frozenset({"effective_to", "updated_at"})
    __indexes__ = (
        Index(asc("organization_id", "employee_id", "effective_from"), name="ix_employee_jobs_employee"),
        Index(asc("organization_id", "manager_employee_id"), name="ix_employee_jobs_manager"),
        Index(asc("organization_id", "department_id"), name="ix_employee_jobs_department_id"),
        Index(asc("organization_id", "designation_id"), name="ix_employee_jobs_designation_id"),
        Index(asc("organization_id", "location_id"), name="ix_employee_jobs_location_id"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "employment_type": allowed(EmploymentType),
        "change_reason": allowed(JobChangeReason),
        "notes": {"maxLength": 1000},
    }
    __expr_rules__ = (
        {"$or": [{"$eq": ["$effective_to", None]}, {"$gt": ["$effective_to", "$effective_from"]}]},
        {"$ne": ["$manager_employee_id", "$employee_id"]},
    )

    employee_id: uuid.UUID
    effective_from: date
    # Exclusive end; None while the row is open-ended.
    effective_to: date | None = None
    department_id: uuid.UUID
    designation_id: uuid.UUID
    location_id: uuid.UUID
    manager_employee_id: uuid.UUID | None = None
    employment_type: str
    change_reason: str
    notes: str | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class EmployeePersonal(Document):
    """The `personal` tier (docs/security-architecture.md §2), kept apart from the employee
    document so `internal` reads never load it. Its `_id` is the employee's."""

    __collection__ = "employee_personal"
    __id_field__ = "employee_id"
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "personal_email": _EMAIL,
        "personal_phone": {"minLength": 1, "maxLength": 32},
        "current_address": {"maxProperties": 12},
        "permanent_address": {"maxProperties": 12},
        "version": {"minimum": 1},
    }

    employee_id: uuid.UUID
    organization_id: uuid.UUID | None = None
    date_of_birth: date | None = None
    personal_email: str | None = None
    personal_phone: str | None = None
    current_address: dict[str, str] | None = None
    permanent_address: dict[str, str] | None = None
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class EmergencyContact(Record):
    """The `personal` tier: up to five per person, in priority order."""

    __collection__ = "emergency_contacts"
    __indexes__ = (
        Index(
            asc("organization_id", "employee_id", "priority"),
            name="uq_emergency_contacts_priority",
            unique=True,
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "priority": {"minimum": 1, "maximum": 5},
        "name": {"minLength": 1, "maxLength": 200},
        "relationship": {"minLength": 1, "maxLength": 100},
        "phone": {"pattern": r"^\+?[0-9 ()-]{5,30}$"},
    }

    employee_id: uuid.UUID
    priority: int
    name: str
    relationship: str
    phone: str
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class EmployeeStatusChange(Record):
    """Append-only."""

    __collection__ = "employee_status_history"
    __append_only__ = True
    __indexes__ = (
        Index(asc("organization_id", "employee_id", "created_at"), name="ix_employee_status_history"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "from_status": allowed(EmployeeStatus),
        "to_status": allowed(EmployeeStatus),
        "reason": {"minLength": 1, "maxLength": 500},
    }
    __expr_rules__ = ({"$ne": ["$from_status", "$to_status"]},)

    employee_id: uuid.UUID
    from_status: str
    to_status: str
    effective_date: date
    reason: str
    changed_by: uuid.UUID
    created_at: datetime = dataclasses.field(default_factory=utcnow)
