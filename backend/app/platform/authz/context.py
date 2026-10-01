"""Who is acting: the authenticated account, its session and its effective permissions.

An Actor is built once per request from PostgreSQL (session, role assignments, derived
roles); nothing is cached between requests, so a revocation or an expired assignment applies
on the next request (docs/authorization-model.md §1).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SessionScope(StrEnum):
    FULL = "full"
    # After a recovery-code sign-in: only MFA enrolment until a new factor is confirmed.
    MFA_ENROLMENT = "mfa_enrolment"


@dataclass(frozen=True, slots=True)
class Constraint:
    """Narrows `all` scope to employees in a department and/or location. Both None: unrestricted."""

    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None

    @property
    def unrestricted(self) -> bool:
        return self.department_id is None and self.location_id is None


UNRESTRICTED = Constraint()


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: uuid.UUID
    session_id: uuid.UUID
    session_scope: SessionScope
    employee_id: uuid.UUID | None
    # Permission key -> the constraints it is held under (one per granting assignment).
    grants: Mapping[str, tuple[Constraint, ...]] = field(default_factory=dict)
    role_keys: frozenset[str] = frozenset()
    step_up_at: datetime | None = None

    def holds(self, key: str) -> bool:
        return key in self.grants

    @property
    def permission_keys(self) -> frozenset[str]:
        return frozenset(self.grants)

    @property
    def enrolment_only(self) -> bool:
        return self.session_scope is SessionScope.MFA_ENROLMENT
