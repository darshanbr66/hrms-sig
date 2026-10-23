"""The org module's interface for other modules (docs/architecture.md §4).

- `unit_names` names departments, designations and locations for display (organization
  records hold no personal data).
- `inactive_units` tells a caller which of the units it is about to place someone in are
  missing or archived.
- `location_time_zones` gives the IANA time zone of each location, for work dates.
- `missing_units` names the department or location references that point at nothing, for
  records that refer to them (there are no foreign keys in MongoDB).
- `active_locations` names the active locations among some IDs (code and name), for the
  offices a person may clock at (ADR-031).
- `clear_department_head` removes a person as head of any department, for the permanent erasure
  of their data.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from app.modules.org.models import Department, Designation, Location, OrgUnitStatus
from app.platform.db import Session
from app.platform.erasure import Erased, Outcome


@dataclass(frozen=True, slots=True)
class UnitNames:
    departments: dict[uuid.UUID, str]
    designations: dict[uuid.UUID, str]
    locations: dict[uuid.UUID, str]


async def unit_names(
    session: Session,
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
        return {row.id: row.name for row in await session.find(model, {"id": {"$in": list(set(ids))}})}

    return UnitNames(
        departments=await names(Department, department_ids),
        designations=await names(Designation, designation_ids),
        locations=await names(Location, location_ids),
    )


async def inactive_units(
    session: Session, *, department_id: uuid.UUID, designation_id: uuid.UUID, location_id: uuid.UUID
) -> list[str]:
    """The fields (`department_id`, ...) naming a unit that does not exist or is archived.

    The units are locked until the caller commits, so archiving one (which locks it and then
    counts the people placed in it) and this placement never both succeed: one of the two meets
    a write conflict and changes nothing."""
    found: list[str] = []
    units: tuple[tuple[str, type[Department | Designation | Location], uuid.UUID], ...] = (
        ("department_id", Department, department_id),
        ("designation_id", Designation, designation_id),
        ("location_id", Location, location_id),
    )
    for field, model, unit_id in units:
        unit = await session.get(model, unit_id, for_update=True)
        if unit is None or unit.status != OrgUnitStatus.ACTIVE.value:
            found.append(field)
    return found


async def location_time_zones(session: Session, location_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not location_ids:
        return {}
    locations = await session.find(Location, {"id": {"$in": list(set(location_ids))}})
    return {location.id: location.time_zone for location in locations}


async def missing_units(
    session: Session, *, department_id: uuid.UUID | None = None, location_id: uuid.UUID | None = None
) -> list[str]:
    """The fields (`department_id`, `location_id`) naming a unit that does not exist."""
    missing: list[str] = []
    if department_id is not None and await session.get(Department, department_id) is None:
        missing.append("department_id")
    if location_id is not None and await session.get(Location, location_id) is None:
        missing.append("location_id")
    return missing


async def active_locations(
    session: Session, location_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    """Code and name of each active location among `location_ids`; archived or unknown ones
    are left out."""
    if not location_ids:
        return {}
    found = await session.find(
        Location, {"id": {"$in": sorted(location_ids)}, "status": OrgUnitStatus.ACTIVE.value}
    )
    return {location.id: (location.code, location.name) for location in found}


async def clear_department_head(session: Session, employee_id: uuid.UUID) -> Erased:
    result = Erased()
    headed = await session.find(Department, {"head_employee_id": employee_id})
    for department in headed:
        department.head_employee_id = None
        department.version += 1
    await session.flush()
    result.add(
        "org.department_head", Outcome.DELETED, len(headed), "The person named as a department's head."
    )
    return result
