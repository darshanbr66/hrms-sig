"""Request and response models for roles and role assignments."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints


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


class RoleChoice(_Model):
    role_key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    valid_until: AwareDatetime | None = None


class AssignmentBatchCreate(_Model):
    """Several roles for one account, with one reason; all are assigned or none."""

    roles: Annotated[list[RoleChoice], Field(min_length=1, max_length=16)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AssignmentView(_Model):
    id: uuid.UUID
    role_key: str
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    valid_from: datetime
    valid_until: datetime | None
    granted_by: uuid.UUID | None
    grant_reason: str
    # The access request it came from (elevation, break-glass, super admin assignment).
    grant_request_id: uuid.UUID | None
    created_at: datetime


class AssignmentList(_Model):
    items: list[AssignmentView]


# --- grant requests -----------------------------------------------------------------------------

GrantKindValue = Literal["elevation", "super_admin_assignment", "break_glass"]
GrantStatusValue = Literal["pending", "approved", "rejected", "expired", "active", "ended", "revoked"]
type Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class UserRef(_Model):
    id: uuid.UUID
    # Work email (`public_internal`); null only if the account no longer exists.
    email: str | None


class GrantRequestView(_Model):
    id: uuid.UUID
    kind: GrantKindValue
    # As of now: a pending request past its 24-hour decision window is `expired`, an active
    # one past its end is `ended`, even before the expiry job records it.
    status: GrantStatusValue
    subject: UserRef
    role_key: str
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    reason: str
    duration_minutes: int | None
    requested_by: UserRef
    requested_at: datetime
    decide_by: datetime | None
    decided_by: UserRef | None
    decided_at: datetime | None
    decision_note: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    revoked_by: UserRef | None
    revoked_at: datetime | None
    acknowledged_by: UserRef | None
    acknowledged_at: datetime | None


class GrantRequestPage(_Model):
    items: list[GrantRequestView]
    next_cursor: uuid.UUID | None


class ElevationCreate(_Model):
    role_key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    reason: Reason
    # Default 2 hours, at most 8 (docs/authorization-model.md §4.3).
    duration_minutes: Annotated[int, Field(ge=1, le=480)] = 120
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None


class BreakGlassCreate(_Model):
    reason: Reason
    # At most 1 hour.
    duration_minutes: Annotated[int, Field(ge=1, le=60)] = 60


class GrantDecision(_Model):
    note: Reason | None = None


class CurrentElevation(_Model):
    """The elevation or break-glass that applies to the signed-in account now, if any."""

    elevation: GrantRequestView | None
