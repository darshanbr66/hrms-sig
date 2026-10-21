"""Organization units: locations, departments and designations (docs/database-design.md §4.3).

Codes are unique per organization, compared without regard to case. Allowed values and
lengths are part of each collection's validator (`__field_rules__`).
"""

import dataclasses
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import CASE_INSENSITIVE, Index, Record, asc, utcnow


class OrgUnitStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


_CODE = {"minLength": 1, "maxLength": 32}
_NAME = {"minLength": 1, "maxLength": 200}


@dataclasses.dataclass(kw_only=True, eq=False)
class Location(Record):
    __collection__ = "locations"
    __indexes__ = (
        Index(
            asc("organization_id", "code"), name="uq_locations_code", unique=True, collation=CASE_INSENSITIVE
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "code": _CODE,
        "name": _NAME,
        "time_zone": {"maxLength": 64},
        "country_code": {"pattern": "^[A-Z]{2}$"},
        "address": {"maxLength": 1000},
    }

    code: str
    name: str
    # IANA zone name; validated by the service that writes locations.
    time_zone: str
    country_code: str
    address: str | None = None
    status: OrgUnitStatus = OrgUnitStatus.ACTIVE
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class Department(Record):
    __collection__ = "departments"
    __indexes__ = (
        Index(
            asc("organization_id", "code"),
            name="uq_departments_code",
            unique=True,
            collation=CASE_INSENSITIVE,
        ),
        Index(asc("organization_id", "parent_id"), name="ix_departments_parent_id"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "code": _CODE,
        "name": _NAME,
        "description": {"minLength": 1, "maxLength": 1000},
    }

    code: str
    name: str
    # What the department does.
    description: str | None = None
    parent_id: uuid.UUID | None = None
    head_employee_id: uuid.UUID | None = None
    status: OrgUnitStatus = OrgUnitStatus.ACTIVE
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class Designation(Record):
    __collection__ = "designations"
    __indexes__ = (
        Index(
            asc("organization_id", "code"),
            name="uq_designations_code",
            unique=True,
            collation=CASE_INSENSITIVE,
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "code": _CODE,
        "name": _NAME,
        "level": {"minimum": 0},
    }

    code: str
    name: str
    level: int | None = None
    status: OrgUnitStatus = OrgUnitStatus.ACTIVE
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)
