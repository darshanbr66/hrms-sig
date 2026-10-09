"""Data access for roles, permissions and role assignments.

An assignment applies while it is not revoked and `valid_from <= now < valid_until` (or has no
end), so a temporary assignment stops applying without any job (docs/authorization-model.md §5).
A lapsed assignment (its end has passed) is closed by recording `revoked_at = valid_until`
with no `revoked_by`: closing frees the unique slot, so the same role can be given again,
and keeps the history exact.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access.models import (
    GrantKind,
    GrantStatus,
    PermissionRow,
    Role,
    RoleGrantRequest,
    RolePermission,
    UserRole,
)

NO_UNIT = uuid.UUID(int=0)


def assignment_applies(now: datetime) -> ColumnElement[bool]:
    return and_(
        UserRole.revoked_at.is_(None),
        UserRole.valid_from <= now,
        or_(UserRole.valid_until.is_(None), UserRole.valid_until > now),
    )


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


async def assigned_grants(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> list[GrantRow]:
    query = (
        select(
            PermissionRow.key,
            Role.key,
            UserRole.department_id,
            UserRole.location_id,
            UserRole.grant_request_id,
            UserRole.valid_until.is_not(None),
        )
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(PermissionRow, PermissionRow.id == RolePermission.permission_id)
        .where(UserRole.user_id == user_id, assignment_applies(now))
    )
    return [GrantRow(*row) for row in (await session.execute(query)).all()]


async def derived_grants(session: AsyncSession, role_keys: Collection[str]) -> list[GrantRow]:
    if not role_keys:
        return []
    query = (
        select(PermissionRow.key, Role.key)
        .select_from(Role)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(PermissionRow, PermissionRow.id == RolePermission.permission_id)
        .where(Role.key.in_(role_keys), Role.is_derived.is_(True))
    )
    return [
        GrantRow(permission, role, None, None) for permission, role in (await session.execute(query)).all()
    ]


async def roles(session: AsyncSession) -> Sequence[Role]:
    return list((await session.execute(select(Role).order_by(Role.key))).scalars())


async def role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return await session.get(Role, role_id)


async def role_by_key(session: AsyncSession, key: str) -> Role | None:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one_or_none()


async def role_permission_keys(
    session: AsyncSession, role_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    query = (
        select(RolePermission.role_id, PermissionRow.key)
        .join(PermissionRow, PermissionRow.id == RolePermission.permission_id)
        .where(RolePermission.role_id.in_(role_ids))
        .order_by(PermissionRow.key)
    )
    result: dict[uuid.UUID, list[str]] = {role_id: [] for role_id in role_ids}
    for role_id, key in (await session.execute(query)).all():
        result[role_id].append(key)
    return result


async def active_assignments(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> list[tuple[UserRole, str]]:
    query = (
        select(UserRole, Role.key)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, assignment_applies(now))
        .order_by(UserRole.id)
    )
    return [(row.UserRole, row.key) for row in (await session.execute(query)).all()]


async def lock_assignment(session: AsyncSession, assignment_id: uuid.UUID) -> UserRole | None:
    query = (
        select(UserRole)
        .where(UserRole.id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def lock_role_holders(session: AsyncSession, role_id: uuid.UUID, now: datetime) -> list[UserRole]:
    """Every applying assignment of a role, locked in ID order (so concurrent callers cannot
    deadlock), so a check on how many holders remain is not raced by a concurrent change."""
    query = (
        select(UserRole)
        .where(UserRole.role_id == role_id, assignment_applies(now))
        .order_by(UserRole.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(query)).scalars())


async def role_ever_assigned(session: AsyncSession, role_id: uuid.UUID) -> bool:
    query = select(exists().where(UserRole.role_id == role_id))
    return bool((await session.execute(query)).scalar_one())


async def holder_ids(session: AsyncSession, role_keys: Collection[str], now: datetime) -> list[uuid.UUID]:
    """Accounts an assignment of one of these roles applies to now (the caller filters by
    account status)."""
    query = (
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.key.in_(role_keys), assignment_applies(now))
        .distinct()
        .order_by(UserRole.user_id)
    )
    return list((await session.execute(query)).scalars())


async def close_lapsed(
    session: AsyncSession,
    now: datetime,
    *,
    user_id: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Record the end of assignments whose `valid_until` has passed: everyone's (the expiry
    job), or one person's assignments of one role and constraint (before giving it again)."""
    statement = update(UserRole).where(
        UserRole.revoked_at.is_(None), UserRole.valid_until.is_not(None), UserRole.valid_until <= now
    )
    if user_id is not None:
        statement = statement.where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
            func.coalesce(UserRole.department_id, NO_UNIT) == (department_id or NO_UNIT),
            func.coalesce(UserRole.location_id, NO_UNIT) == (location_id or NO_UNIT),
        )
    result = await session.execute(
        statement.values(revoked_at=UserRole.valid_until).returning(UserRole.id),
        execution_options={"synchronize_session": False},
    )
    return list(result.scalars())


# --- grant requests -------------------------------------------------------------------------


async def lock_request(session: AsyncSession, request_id: uuid.UUID) -> RoleGrantRequest | None:
    query = (
        select(RoleGrantRequest)
        .where(RoleGrantRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def list_requests(
    session: AsyncSession, *, statuses: Collection[str] | None, before: uuid.UUID | None, limit: int
) -> tuple[list[RoleGrantRequest], uuid.UUID | None]:
    """Newest first (UUIDv7 IDs are time-ordered), keyset-paginated."""
    query = select(RoleGrantRequest).order_by(RoleGrantRequest.id.desc()).limit(limit + 1)
    if statuses:
        query = query.where(RoleGrantRequest.status.in_(statuses))
    if before is not None:
        query = query.where(RoleGrantRequest.id < before)
    rows = list((await session.execute(query)).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (rows[-1].id if more and rows else None)


async def lock_due_requests(
    session: AsyncSession, *, now: datetime, requested_before: datetime, limit: int
) -> list[RoleGrantRequest]:
    """Pending requests older than the decision window and active ones past their end, locked
    in ID order; rows another transaction holds are skipped."""
    due = or_(
        and_(
            RoleGrantRequest.status == GrantStatus.PENDING.value,
            RoleGrantRequest.requested_at <= requested_before,
        ),
        and_(RoleGrantRequest.status == GrantStatus.ACTIVE.value, RoleGrantRequest.ends_at <= now),
    )
    query = (
        select(RoleGrantRequest)
        .where(due)
        .order_by(RoleGrantRequest.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(query)).scalars())


async def lock_open_requests_of(session: AsyncSession, user_id: uuid.UUID) -> list[RoleGrantRequest]:
    """A person's pending and active elevation and break-glass requests, locked in ID order."""
    query = (
        select(RoleGrantRequest)
        .where(
            RoleGrantRequest.subject_user_id == user_id,
            RoleGrantRequest.kind.in_([GrantKind.ELEVATION.value, GrantKind.BREAK_GLASS.value]),
            RoleGrantRequest.status.in_([GrantStatus.PENDING.value, GrantStatus.ACTIVE.value]),
        )
        .order_by(RoleGrantRequest.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(query)).scalars())


async def lock_request_assignment(session: AsyncSession, request_id: uuid.UUID) -> UserRole | None:
    query = (
        select(UserRole)
        .where(UserRole.grant_request_id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def request_kind(session: AsyncSession, request_id: uuid.UUID) -> GrantKind | None:
    kind = (
        await session.execute(select(RoleGrantRequest.kind).where(RoleGrantRequest.id == request_id))
    ).scalar_one_or_none()
    return GrantKind(kind) if kind is not None else None
