"""Request and response models for locations, departments and designations.

Organization records hold no personal data. Codes are unique regardless of case.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type Code = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^\S+$")
]
type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
type StatusValue = Literal["active", "archived"]
type Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- locations ----------------------------------------------------------------------------------


class LocationCreate(_Request):
    code: Code
    name: Name
    # IANA zone name, for example "Asia/Kolkata".
    time_zone: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    country_code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    address: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None


class LocationUpdate(_Request):
    name: Name | None = None
    time_zone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    country_code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    address: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None
    status: StatusValue | None = None


class LocationView(_Response):
    id: uuid.UUID
    code: str
    name: str
    time_zone: str
    country_code: str
    address: str | None
    status: StatusValue
    version: int
    created_at: datetime
    updated_at: datetime


class LocationList(_Response):
    items: list[LocationView]


# --- departments --------------------------------------------------------------------------------


class DepartmentCreate(_Request):
    code: Code
    name: Name
    description: Description | None = None
    parent_id: uuid.UUID | None = None
    head_employee_id: uuid.UUID | None = None


class DepartmentUpdate(_Request):
    name: Name | None = None
    description: Description | None = None
    parent_id: uuid.UUID | None = None
    head_employee_id: uuid.UUID | None = None
    status: StatusValue | None = None


class DepartmentView(_Response):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    parent_id: uuid.UUID | None
    head_employee_id: uuid.UUID | None
    # The head's display name (a directory field), when there is a head.
    head_name: str | None
    status: StatusValue
    version: int
    created_at: datetime
    updated_at: datetime


class DepartmentList(_Response):
    items: list[DepartmentView]


# --- designations -------------------------------------------------------------------------------


class DesignationCreate(_Request):
    code: Code
    name: Name
    level: Annotated[int, Field(ge=0, le=100)] | None = None


class DesignationUpdate(_Request):
    name: Name | None = None
    level: Annotated[int, Field(ge=0, le=100)] | None = None
    status: StatusValue | None = None


class DesignationView(_Response):
    id: uuid.UUID
    code: str
    name: str
    level: int | None
    status: StatusValue
    version: int
    created_at: datetime
    updated_at: datetime


class DesignationList(_Response):
    items: list[DesignationView]
