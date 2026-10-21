"""Role assignments and grant requests (docs/database-design.md §4.2).

The role and permission catalog lives in code (`app.platform.authz.roles`, `catalog`); a role
is identified by its key, and by a stable identifier derived from it (`role_id`). Assignments
are revoked, never deleted, and never name a derived role. Grant requests are history: what
was asked for never changes, statuses only move forward, and a decision, revocation or
acknowledgement is recorded once (update rules below). The rules within one document are part
of each collection's validator, as the CHECK constraints were. Whether an assignment matches
the request it comes from is checked before every commit that adds one (`invariants.py`).
"""

import dataclasses
import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Final

from app.platform.authz.roles import ROLES
from app.platform.db import Index, Record, allowed, asc, utcnow

ROLE_NAMESPACE: Final = uuid.UUID("6d6b6f2e-6872-6d73-2e72-6f6c65732e31")


def role_id(key: str) -> uuid.UUID:
    """The stable identifier of a role, the same in every installation."""
    return uuid.uuid5(ROLE_NAMESPACE, key)


_ASSIGNABLE: Final = tuple(role.key.value for role in ROLES if not role.derived)


class GrantKind(StrEnum):
    ELEVATION = "elevation"
    SUPER_ADMIN_ASSIGNMENT = "super_admin_assignment"
    BREAK_GLASS = "break_glass"


class GrantStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"  # a super admin assignment: the role is held with no end
    REJECTED = "rejected"
    EXPIRED = "expired"  # not decided within the decision window
    ACTIVE = "active"  # an elevation or break-glass inside its window
    ENDED = "ended"  # its window is over
    REVOKED = "revoked"  # withdrawn while pending, or ended early


ELEVATION_MAX: Final = timedelta(hours=8)
BREAK_GLASS_MAX: Final = timedelta(hours=1)
_MS = 1000


@dataclasses.dataclass(kw_only=True, eq=False)
class UserRole(Record):
    __collection__ = "user_roles"
    __indexes__ = (
        Index(
            asc("organization_id", "user_id", "role_key", "department_id", "location_id"),
            name="uq_user_roles_active_assignment",
            unique=True,
            partial={"revoked_at": {"$type": "null"}},
        ),
        Index(asc("organization_id", "user_id", "revoked_at"), name="ix_user_roles_user_id"),
        Index(asc("organization_id", "role_key", "revoked_at"), name="ix_user_roles_role_key"),
        Index(
            asc("organization_id", "grant_request_id"),
            name="uq_user_roles_grant_request_id",
            unique=True,
            partial={"grant_request_id": {"$type": "binData"}},
        ),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "role_key": allowed(_ASSIGNABLE),
        "grant_reason": {"minLength": 1, "maxLength": 500},
    }
    __expr_rules__ = (
        {"$or": [{"$eq": ["$valid_until", None]}, {"$gt": ["$valid_until", "$valid_from"]}]},
        {
            "$or": [
                {"$eq": ["$granted_by", None]},
                {"$ne": ["$granted_by", "$user_id"]},
                {"$ne": ["$grant_request_id", None]},
            ]
        },
        {"$or": [{"$eq": ["$revoked_by", None]}, {"$ne": ["$revoked_at", None]}]},
    )
    # Only the end of an assignment is recorded later, once.
    # History: an assignment ends by revocation and is never deleted.
    __deletable__ = False
    __mutable__ = frozenset({"revoked_at", "revoked_by"})
    __set_once__ = frozenset({"revoked_at", "revoked_by"})

    user_id: uuid.UUID
    role_id: uuid.UUID
    role_key: str
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    valid_from: datetime = dataclasses.field(default_factory=utcnow)
    valid_until: datetime | None = None
    granted_by: uuid.UUID | None = None
    grant_reason: str
    revoked_at: datetime | None = None
    revoked_by: uuid.UUID | None = None
    # The request (elevation, super admin assignment, break-glass) the assignment came from.
    grant_request_id: uuid.UUID | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)


def _is(field: str, *values: str) -> dict[str, Any]:
    return {"$in": [f"${field}", list(values)]}


def _null(field: str) -> dict[str, Any]:
    return {"$eq": [f"${field}", None]}


@dataclasses.dataclass(kw_only=True, eq=False)
class RoleGrantRequest(Record):
    __collection__ = "role_grant_requests"
    # History: a request ends in a final status and is never deleted.
    __deletable__ = False
    __indexes__ = (
        Index(
            asc("organization_id", "subject_user_id", "kind"),
            name="uq_role_grant_requests_pending",
            unique=True,
            partial={"status": "pending"},
        ),
        Index(
            asc("organization_id", "subject_user_id"),
            name="uq_role_grant_requests_active",
            unique=True,
            partial={"status": "active"},
        ),
        Index(asc("organization_id", "status", "_id"), name="ix_role_grant_requests_status"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "kind": allowed(GrantKind),
        "status": allowed(GrantStatus),
        "reason": {"minLength": 1, "maxLength": 500},
        "decision_note": {"minLength": 1, "maxLength": 500},
    }
    __expr_rules__ = (
        # Statuses per kind.
        {
            "$or": [
                {
                    "$and": [
                        _is("kind", "elevation"),
                        _is("status", "pending", "active", "rejected", "expired", "ended", "revoked"),
                    ]
                },
                {
                    "$and": [
                        _is("kind", "super_admin_assignment"),
                        _is("status", "pending", "approved", "rejected", "expired", "revoked"),
                    ]
                },
                {"$and": [_is("kind", "break_glass"), _is("status", "active", "ended", "revoked")]},
            ]
        },
        # Roles per kind.
        {
            "$or": [
                {
                    "$and": [
                        _is("kind", "elevation"),
                        {"$not": [_is("role_key", "super_admin", "employee", "manager")]},
                    ]
                },
                {"$and": [_is("kind", "super_admin_assignment"), _is("role_key", "super_admin")]},
                {
                    "$and": [
                        _is("kind", "break_glass"),
                        _is("role_key", "system_admin"),
                        _null("department_id"),
                        _null("location_id"),
                    ]
                },
            ]
        },
        # Who asks.
        {
            "$or": [
                {
                    "$and": [
                        _is("kind", "elevation", "break_glass"),
                        {"$eq": ["$requested_by", "$subject_user_id"]},
                    ]
                },
                {
                    "$and": [
                        _is("kind", "super_admin_assignment"),
                        {"$ne": ["$requested_by", "$subject_user_id"]},
                    ]
                },
            ]
        },
        # Who decides: never the requester or the subject (separation of duties).
        {
            "$or": [
                _null("decided_by"),
                {
                    "$and": [
                        {"$ne": ["$decided_by", "$requested_by"]},
                        {"$ne": ["$decided_by", "$subject_user_id"]},
                    ]
                },
            ]
        },
        {"$eq": [_null("decided_by"), _null("decided_at")]},
        {"$or": [_null("decision_note"), {"$ne": ["$decided_by", None]}]},
        {"$or": [{"$ne": ["$kind", "break_glass"]}, _null("decided_by")]},
        {"$or": [{"$not": [_is("status", "approved", "rejected")]}, {"$ne": ["$decided_by", None]}]},
        # An elevation is active only once someone else approved it.
        {
            "$or": [
                {"$ne": ["$kind", "elevation"]},
                {"$ne": ["$status", "active"]},
                {"$ne": ["$decided_by", None]},
            ]
        },
        # Durations, in milliseconds.
        {
            "$or": [
                {
                    "$and": [
                        _is("kind", "elevation"),
                        {"$gt": ["$requested_duration", 0]},
                        {"$lte": ["$requested_duration", int(ELEVATION_MAX.total_seconds() * _MS)]},
                    ]
                },
                {
                    "$and": [
                        _is("kind", "break_glass"),
                        {"$gt": ["$requested_duration", 0]},
                        {"$lte": ["$requested_duration", int(BREAK_GLASS_MAX.total_seconds() * _MS)]},
                    ]
                },
                {"$and": [_is("kind", "super_admin_assignment"), _null("requested_duration")]},
            ]
        },
        # The window.
        {
            "$or": [
                {
                    "$and": [
                        _is("kind", "super_admin_assignment"),
                        _null("ends_at"),
                        {"$eq": [{"$ne": ["$status", "approved"]}, _null("starts_at")]},
                    ]
                },
                {
                    "$and": [
                        _is("kind", "elevation", "break_glass"),
                        {"$eq": [_null("starts_at"), _null("ends_at")]},
                        {
                            "$eq": [
                                _null("starts_at"),
                                {
                                    "$or": [
                                        _is("status", "pending", "rejected", "expired"),
                                        {
                                            "$and": [
                                                {"$eq": ["$status", "revoked"]},
                                                _null("decided_by"),
                                                {"$eq": ["$kind", "elevation"]},
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                        {
                            "$or": [
                                _null("ends_at"),
                                {"$eq": [{"$subtract": ["$ends_at", "$starts_at"]}, "$requested_duration"]},
                            ]
                        },
                    ]
                },
            ]
        },
        {
            "$and": [
                {"$eq": [_null("revoked_by"), _null("revoked_at")]},
                {"$eq": [{"$eq": ["$status", "revoked"]}, {"$ne": ["$revoked_at", None]}]},
            ]
        },
        {"$eq": [_null("acknowledged_by"), _null("acknowledged_at")]},
        {
            "$or": [
                _null("acknowledged_by"),
                {"$and": [{"$eq": ["$kind", "break_glass"]}, {"$ne": ["$acknowledged_by", "$requested_by"]}]},
            ]
        },
    )
    # What was asked for never changes; a decision, revocation or acknowledgement is recorded
    # once; statuses only move forward.
    __mutable__ = frozenset(
        {
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "starts_at",
            "ends_at",
            "revoked_by",
            "revoked_at",
            "acknowledged_by",
            "acknowledged_at",
            "updated_at",
        }
    )
    __set_once__ = frozenset(
        {
            "decided_by",
            "decided_at",
            "decision_note",
            "starts_at",
            "ends_at",
            "revoked_by",
            "revoked_at",
            "acknowledged_by",
            "acknowledged_at",
        }
    )
    __transitions__: ClassVar[dict[str, dict[str, frozenset[str]]]] = {
        "status": {
            "pending": frozenset({"approved", "active", "rejected", "expired", "revoked"}),
            "active": frozenset({"ended", "revoked"}),
        }
    }

    kind: str
    subject_user_id: uuid.UUID
    role_id: uuid.UUID
    role_key: str
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    reason: str
    requested_duration: timedelta | None = None
    requested_by: uuid.UUID
    requested_at: datetime = dataclasses.field(default_factory=utcnow)
    status: str
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    revoked_by: uuid.UUID | None = None
    revoked_at: datetime | None = None
    acknowledged_by: uuid.UUID | None = None
    acknowledged_at: datetime | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)
