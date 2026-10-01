"""Request and response models for roles and role assignments."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleView(_Model):
    id: uuid.UUID
    key: str
    name: str
    description: str
    is_derived: bool
    permissions: list[str]


class RoleList(_Model):
    items: list[RoleView]


class AssignmentCreate(_Model):
    role_key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    valid_until: AwareDatetime | None = None


class AssignmentView(_Model):
    id: uuid.UUID
    role_key: str
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    valid_from: datetime
    valid_until: datetime | None
    granted_by: uuid.UUID | None
    grant_reason: str
    created_at: datetime


class AssignmentList(_Model):
    items: list[AssignmentView]
