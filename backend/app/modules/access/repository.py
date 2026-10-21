"""Data access for roles, permissions and role assignments.

Roles and their permissions come from the code catalog (`app.platform.authz.roles`); each role
has a stable identifier (`role_id`). An assignment applies while it is not revoked and
`valid_from <= now < valid_until` (or has no end), so a temporary assignment stops applying
without any job (docs/authorization-model.md §5). A lapsed assignment (its end has passed) is
closed by recording `revoked_at = valid_until` with no `revoked_by`: closing frees the unique
slot, so the same role can be given again, and keeps the history exact.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.modules.access.invariants import require_grant_match
from app.modules.access.models import GrantKind, GrantStatus, RoleGrantRequest, UserRole, role_id
from app.platform.authz.roles import ROLE_DEFINITIONS, ROLES, RoleDefinition, RoleKey
from app.platform.db import Session


@dataclass(frozen=True, slots=True)
class Role:
    """A role of the catalog, as the API shows it."""

    id: uuid.UUID
    key: str
    name: str
    description: str
    is_system: bool
    is_derived: bool


def _role(definition: RoleDefinition) -> Role:
    return Role(
        id=role_id(definition.key.value),
        key=definition.key.value,
        name=definition.name,
        description=definition.description,
        is_system=True,
        is_derived=definition.derived,
    )


_BY_ID = {role_id(definition.key.value): definition for definition in ROLES}


def assignment_applies(now: datetime) -> dict[str, Any]:
    return {
        "revoked_at": None,
        "valid_from": {"$lte": now},
        "$or": [{"valid_until": None}, {"valid_until": {"$gt": now}}],
    }


@dataclass(frozen=True, slots=True)
class GrantRow:
    permission_key: str
    role_key: str
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    # Set for an assignment that came from a grant request; `time_bound` marks an elevation
    # or break-glass (a super admin assignment has no end).
    grant_request_id: uuid.UUID | None = None
    time_bound: bool = False


async def assigned_grants(session: Session, user_id: uuid.UUID, now: datetime) -> list[GrantRow]:
    rows: list[GrantRow] = []
    for assignment in await session.find(UserRole, {"user_id": user_id, **assignment_applies(now)}):
        definition = ROLE_DEFINITIONS.get(RoleKey(assignment.role_key))
        if definition is None or definition.derived:
            continue
        rows.extend(
            GrantRow(
                permission,
                assignment.role_key,
                assignment.department_id,
                assignment.location_id,
                assignment.grant_request_id,
                assignment.valid_until is not None,
            )
            for permission in sorted(definition.permissions)
        )
    return rows


async def derived_grants(session: Session, role_keys: Collection[str]) -> list[GrantRow]:
    rows: list[GrantRow] = []
    for key in role_keys:
        definition = ROLE_DEFINITIONS.get(RoleKey(key))
        if definition is not None and definition.derived:
            rows.extend(
                GrantRow(permission, key, None, None) for permission in sorted(definition.permissions)
            )
    return rows


async def roles(session: Session) -> Sequence[Role]:
    return sorted((_role(definition) for definition in ROLES), key=lambda item: item.key)


async def role(session: Session, identifier: uuid.UUID) -> Role | None:
    definition = _BY_ID.get(identifier)
    return _role(definition) if definition is not None else None


async def role_by_key(session: Session, key: str) -> Role | None:
    try:
        definition = ROLE_DEFINITIONS.get(RoleKey(key))
    except ValueError:
        return None
    return _role(definition) if definition is not None else None


async def role_permission_keys(
    session: Session, role_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    return {
        identifier: sorted(_BY_ID[identifier].permissions) if identifier in _BY_ID else []
        for identifier in role_ids
    }


def add_assignment(session: Session, assignment: UserRole) -> None:
    """Add an assignment; whether it matches its grant is checked before the commit."""
    session.add(assignment)
    require_grant_match(session, assignment)


async def active_assignments(
    session: Session, user_id: uuid.UUID, now: datetime
) -> list[tuple[UserRole, str]]:
    assignments = await session.find(
        UserRole, {"user_id": user_id, **assignment_applies(now)}, sort=[("id", 1)]
    )
    return [(assignment, assignment.role_key) for assignment in assignments]


async def lock_assignment(session: Session, assignment_id: uuid.UUID) -> UserRole | None:
    return await session.get(UserRole, assignment_id, for_update=True)


async def lock_role_holders(session: Session, identifier: uuid.UUID, now: datetime) -> list[UserRole]:
    """Every applying assignment of a role, locked in ID order, so a check on how many holders
    remain is not raced by a concurrent change (one of two concurrent changes meets a write
    conflict)."""
    holders = await session.find(
        UserRole, {"role_id": identifier, **assignment_applies(now)}, sort=[("id", 1)]
    )
    locked = [await session.get(UserRole, holder.id, for_update=True) for holder in holders]
    return [holder for holder in locked if holder is not None]


async def role_ever_assigned(session: Session, identifier: uuid.UUID) -> bool:
    return await session.exists(UserRole, {"role_id": identifier})


async def holder_ids(session: Session, role_keys: Collection[str], now: datetime) -> list[uuid.UUID]:
    """Accounts an assignment of one of these roles applies to now (the caller filters by
    account status)."""
    values = await session.distinct(
        UserRole, "user_id", {"role_key": {"$in": list(role_keys)}, **assignment_applies(now)}
    )
    return sorted(values)


async def close_lapsed(
    session: Session,
    now: datetime,
    *,
    user_id: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Record the end of assignments whose `valid_until` has passed: everyone's (the expiry
    job), or one person's assignments of one role and constraint (before giving it again)."""
    query: dict[str, Any] = {"revoked_at": None, "valid_until": {"$ne": None, "$lte": now}}
    if user_id is not None:
        query |= {
            "user_id": user_id,
            "role_id": role_id,
            "department_id": department_id,
            "location_id": location_id,
        }
    lapsed = await session.distinct(UserRole, "id", query)
    if lapsed:
        await session.update_many(
            UserRole, {"id": {"$in": lapsed}, "revoked_at": None}, [{"$set": {"revoked_at": "$valid_until"}}]
        )
    return list(lapsed)


# --- grant requests -------------------------------------------------------------------------


async def lock_request(session: Session, request_id: uuid.UUID) -> RoleGrantRequest | None:
    return await session.get(RoleGrantRequest, request_id, for_update=True)


async def list_requests(
    session: Session, *, statuses: Collection[str] | None, before: uuid.UUID | None, limit: int
) -> tuple[list[RoleGrantRequest], uuid.UUID | None]:
    """Newest first (UUIDv7 IDs are time-ordered), keyset-paginated."""
    query: dict[str, Any] = {}
    if statuses:
        query["status"] = {"$in": list(statuses)}
    if before is not None:
        query["id"] = {"$lt": before}
    rows = await session.find(RoleGrantRequest, query, sort=[("id", -1)], limit=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (rows[-1].id if more and rows else None)


async def lock_due_requests(
    session: Session, *, now: datetime, requested_before: datetime, limit: int
) -> list[RoleGrantRequest]:
    """Pending requests older than the decision window and active ones past their end, locked
    in ID order. A request another transaction is changing makes this one fail with a write
    conflict; the expiry job runs again a minute later."""
    due = {
        "$or": [
            {"status": GrantStatus.PENDING.value, "requested_at": {"$lte": requested_before}},
            {"status": GrantStatus.ACTIVE.value, "ends_at": {"$lte": now}},
        ]
    }
    found = await session.find(RoleGrantRequest, due, sort=[("id", 1)], limit=limit)
    locked = [await session.get(RoleGrantRequest, item.id, for_update=True) for item in found]
    return [item for item in locked if item is not None]


async def lock_open_requests_of(session: Session, user_id: uuid.UUID) -> list[RoleGrantRequest]:
    """A person's pending and active elevation and break-glass requests, locked in ID order."""
    found = await session.find(
        RoleGrantRequest,
        {
            "subject_user_id": user_id,
            "kind": {"$in": [GrantKind.ELEVATION.value, GrantKind.BREAK_GLASS.value]},
            "status": {"$in": [GrantStatus.PENDING.value, GrantStatus.ACTIVE.value]},
        },
        sort=[("id", 1)],
    )
    locked = [await session.get(RoleGrantRequest, item.id, for_update=True) for item in found]
    return [item for item in locked if item is not None]


async def lock_request_assignment(session: Session, request_id: uuid.UUID) -> UserRole | None:
    return await session.find_one(UserRole, {"grant_request_id": request_id}, for_update=True)


async def request_kind(session: Session, request_id: uuid.UUID) -> GrantKind | None:
    request = await session.get(RoleGrantRequest, request_id)
    return GrantKind(request.kind) if request is not None else None
