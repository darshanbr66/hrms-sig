"""The org module's interface for other modules (docs/architecture.md §4).

- `unit_names` names departments, designations and locations for display (organization
  records hold no personal data).
- `inactive_units` tells a caller which of the units it is about to place someone in are
  missing or archived.
- `location_time_zones` gives the IANA time zone of each location, for work dates.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.org.models import Department, Designation, Location, OrgUnitStatus


@dataclass(frozen=True, slots=True)
class UnitNames:
    departments: dict[uuid.UUID, str]
    designations: dict[uuid.UUID, str]
    locations: dict[uuid.UUID, str]


async def unit_names(
    session: AsyncSession,
    *,
    department_ids: Collection[uuid.UUID] = (),
    designation_ids: Collection[uuid.UUID] = (),
    location_ids: Collection[uuid.UUID] = (),
) -> UnitNames:
    async def names(
        model: type[Department | Designation | Location], ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = (await session.execute(select(model.id, model.name).where(model.id.in_(set(ids))))).all()
        return {row.id: row.name for row in rows}

    return UnitNames(
        departments=await names(Department, department_ids),
        designations=await names(Designation, designation_ids),
        locations=await names(Location, location_ids),
    )


async def inactive_units(
    session: AsyncSession, *, department_id: uuid.UUID, designation_id: uuid.UUID, location_id: uuid.UUID
) -> list[str]:
    """The fields (`department_id`, ...) naming a unit that does not exist or is archived.

    The units are locked FOR SHARE until the caller commits, so archiving one (which locks it
    FOR UPDATE and then counts the people placed in it) waits for this placement, or this
    placement waits for the archive and then sees it archived."""
    found: list[str] = []
    for field, model, unit_id in (
        ("department_id", Department, department_id),
        ("designation_id", Designation, designation_id),
        ("location_id", Location, location_id),
    ):
        query = select(model.status).where(model.id == unit_id).with_for_update(read=True)
        status = (await session.execute(query)).scalar_one_or_none()
        if status != OrgUnitStatus.ACTIVE.value:
            found.append(field)
    return found


async def location_time_zones(
    session: AsyncSession, location_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not location_ids:
        return {}
    rows = (
        await session.execute(
            select(Location.id, Location.time_zone).where(Location.id.in_(set(location_ids)))
        )
    ).all()
    return {row.id: row.time_zone for row in rows}
