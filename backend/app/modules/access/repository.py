"""Data access for roles, permissions and role assignments.

An assignment applies while it is not revoked and `valid_from <= now < valid_until` (or has no
end), so a temporary assignment stops applying without any job (docs/authorization-model.md §5).
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access.models import PermissionRow, Role, RolePermission, UserRole


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


async def assigned_grants(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> list[GrantRow]:
    query = (
        select(PermissionRow.key, Role.key, UserRole.department_id, UserRole.location_id)
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
