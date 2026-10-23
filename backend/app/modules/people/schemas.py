"""Request and response models for employee records (docs/api-architecture.md §7).

One model per audience and tier (CLAUDE.md rule 4):

- `DirectoryEntry`: the directory fields every employee may see (product-requirements EMP-6).
- `EmployeeSummary` / `EmployeeView`: the `internal` tier (job details, reporting line,
  joining date, status), for the actor's own record, their team, or HR.
- `PersonalView`: the `personal` tier, on its own sub-resource with its own permissions.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

type StatusValue = Literal["pre_joining", "active", "on_notice", "exited"]
type EmploymentTypeValue = Literal["full_time", "part_time", "contract", "intern"]
type EmployeeSort = Literal["name", "code", "joined_newest", "joined_oldest"]
# docs/product-requirements.md EMP-7: by dates on the day, not by recorded status.
type Employment = Literal["current", "future", "former"]
type JobChangeReasonValue = Literal["transfer", "promotion", "manager_change", "correction", "other"]
type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
type Phone = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
type Email = Annotated[EmailStr, StringConstraints(max_length=254)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnitRef(_Response):
    id: uuid.UUID
    name: str


class PersonRef(_Response):
    id: uuid.UUID
    name: str


# --- directory ----------------------------------------------------------------------------------


class DirectoryEntry(_Response):
    id: uuid.UUID
    name: str
    preferred_name: str | None
    work_email: str | None
    department: UnitRef | None
    designation: UnitRef | None
    location: UnitRef | None


class DirectoryPage(_Response):
    items: list[DirectoryEntry]
    next_cursor: str | None


# --- internal tier ------------------------------------------------------------------------------


class CurrentJob(_Response):
    department: UnitRef
    designation: UnitRef
    location: UnitRef
    manager: PersonRef | None
    employment_type: EmploymentTypeValue
    effective_from: date


class EmployeeSummary(_Response):
    id: uuid.UUID
    employee_code: str
    name: str
    preferred_name: str | None
    work_email: str | None
    status: StatusValue
    date_of_joining: date
    job: CurrentJob | None


class EmployeePage(_Response):
    items: list[EmployeeSummary]
    next_cursor: str | None


type FormerSort = Literal["left_newest", "name"]
type AccountState = Literal["none", "active", "invited", "disabled"]


class FormerAccount(_Response):
    id: uuid.UUID
    email: str
    # `active` after the exit means an administrator account left for a system admin to
    # disable (the exit never disables `system_admin` or `super_admin` accounts).
    status: Literal["active", "invited", "disabled"]


class FormerEmployee(_Response):
    id: uuid.UUID
    employee_code: str
    name: str
    date_of_joining: date
    # Last working day.
    date_of_exit: date
    # The last job held.
    job: CurrentJob | None
    # Null when the person has no account, or the reader may not see accounts.
    account: FormerAccount | None


class FormerEmployeePage(_Response):
    items: list[FormerEmployee]
    next_cursor: str | None
    # Whether the reader may see accounts (`user.read.all`); without it `account` is always null.
    accounts_visible: bool


class EmployeeView(_Response):
    id: uuid.UUID
    employee_code: str
    name: str
    legal_first_name: str
    legal_last_name: str | None
    preferred_name: str | None
    work_email: str | None
    work_phone: str | None
    status: StatusValue
    date_of_joining: date
    date_of_exit: date | None
    job: CurrentJob | None
    version: int
    created_at: datetime
    updated_at: datetime


class JobAssignment(_Request):
    department_id: uuid.UUID
    designation_id: uuid.UUID
    location_id: uuid.UUID
    manager_employee_id: uuid.UUID | None = None
    employment_type: EmploymentTypeValue


class EmployeeCreate(_Request):
    employee_code: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^\S+$")
    ]
    legal_first_name: Name
    legal_last_name: Name | None = None
    preferred_name: Name | None = None
    work_email: Email | None = None
    work_phone: Phone | None = None
    # Status follows from this date: `pre_joining` before it, `active` from it.
    date_of_joining: date
    job: JobAssignment


class EmployeeUpdate(_Request):
    """HR edits of profile fields. Job, status and joining date change through their own routes."""

    employee_code: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^\S+$")
        ]
        | None
    ) = None
    legal_first_name: Name | None = None
    legal_last_name: Name | None = None
    preferred_name: Name | None = None
    work_email: Email | None = None
    work_phone: Phone | None = None


class SelfProfileUpdate(_Request):
    """The fields employees edit themselves (authorization-model §3.2)."""

    preferred_name: Name | None


class JobView(_Response):
    id: uuid.UUID
    effective_from: date
    # Exclusive; null while the row is open-ended.
    effective_to: date | None
    department: UnitRef
    designation: UnitRef
    location: UnitRef
    manager: PersonRef | None
    employment_type: EmploymentTypeValue
    change_reason: Literal["joining", "transfer", "promotion", "manager_change", "correction", "other"]
    # Shown to HR and to the employee; not to managers.
    notes: str | None


class JobList(_Response):
    items: list[JobView]


class JobChange(JobAssignment):
    effective_from: date
    change_reason: JobChangeReasonValue
    notes: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None = (
        None
    )


class StatusChangeCreate(_Request):
    to_status: Literal["active", "on_notice", "exited"]
    effective_date: date
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class StatusChangeView(_Response):
    id: uuid.UUID
    from_status: StatusValue
    to_status: StatusValue
    effective_date: date
    reason: str
    created_at: datetime


class StatusHistory(_Response):
    items: list[StatusChangeView]


class ExitCreate(_Request):
    last_working_day: date
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ExitView(_Response):
    status_change: StatusChangeView
    # none: no account is linked; disabled: disabled now, with its sessions revoked;
    # already_disabled; administrator_left_active: the account holds an administrator role
    # and must be disabled by a system admin.
    account: Literal["none", "disabled", "already_disabled", "administrator_left_active"]
    sessions_revoked: int


class EmergencyContactValue(_Request):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    relationship: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    phone: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9 ()-]{5,30}$")]


class EmergencyContactsUpdate(_Request):
    # In priority order; the whole list is replaced.
    contacts: Annotated[list[EmergencyContactValue], Field(max_length=5)]


class EmergencyContactView(_Response):
    priority: int
    name: str
    relationship: str
    phone: str


class EmergencyContactList(_Response):
    items: list[EmergencyContactView]


# --- summary ------------------------------------------------------------------------------------


class Anniversary(_Response):
    employee: PersonRef
    date: date
    years: int


class StartingSoon(_Response):
    employee: PersonRef
    date_of_joining: date


class RecentJoiner(_Response):
    employee: PersonRef
    date_of_joining: date


class Headcount(_Response):
    """Employees employed today whose current job is in one department, designation or location."""

    id: uuid.UUID
    name: str
    employed: int


class EmployeeStatistics(_Response):
    """Counts over the employees the actor may read (docs/product-requirements.md EMP-7):
    `employed` is the `current` employment of the employee list today; `active` and `on_notice`
    split it by recorded status; `future_joiners` and `former` match `future` and `former`.
    `pre_joining` counts the recorded status, whatever the dates. Headcounts and recent joiners
    count `current` employees only."""

    employed: int
    active: int
    on_notice: int
    pre_joining: int
    future_joiners: int
    former: int
    joined_last_30_days: int
    starting_soon: list[StartingSoon]
    work_anniversaries: list[Anniversary]
    # Joined in the last 30 days, newest first (at most 10).
    recent_joiners: list[RecentJoiner]
    by_department: list[Headcount]
    by_designation: list[Headcount]
    by_location: list[Headcount]


# --- personal tier ------------------------------------------------------------------------------


class Address(_Request):
    line1: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    line2: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = (
        None
    )
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    state: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = (
        None
    )
    postal_code: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)] | None
    ) = None
    country_code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


class PersonalView(_Response):
    date_of_birth: date | None
    personal_email: str | None
    personal_phone: str | None
    current_address: Address | None
    permanent_address: Address | None
    # 0 until the first save.
    version: int = Field(ge=0)


class PersonalUpdate(_Request):
    date_of_birth: date | None = None
    personal_email: Email | None = None
    personal_phone: Phone | None = None
    current_address: Address | None = None
    permanent_address: Address | None = None
