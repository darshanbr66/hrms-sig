"""The people module's interface for other modules (docs/architecture.md §4).

Reporting lines and placement for the authorization engine (docs/authorization-model.md §5, §7):

- `team_member_ids_query` is a SELECT of employee IDs, for embedding in a list query's scope
  filter as `employee_id IN (...)` without materializing the team first.
- `is_team_member` answers the single-record question by walking up the reporting chain.
- `has_direct_reports` decides the derived `manager` role; `is_employed` the derived
  `employee` role.
- `placement` and `placed_employee_ids_query` apply a role assignment's department or
  location constraint.
- `relationships` bundles these for the engine's `Relationships` protocol.

Every function takes the calendar date to resolve on. "Now" is the current date where the
caller decides it; a historical record passes the record's own date, which gives the "team
at the time of the record" rule.
"""

from app.modules.people.repository import (
    first_name,
    has_direct_reports,
    is_employed,
    is_team_member,
    placed_employee_ids_query,
    placement,
    team_member_ids,
    team_member_ids_query,
    work_email,
)


class PeopleRelationships:
    """Implements `app.platform.authz.engine.Relationships`."""

    is_team_member = staticmethod(is_team_member)
    team_member_ids_query = staticmethod(team_member_ids_query)
    placement = staticmethod(placement)
    placed_employee_ids_query = staticmethod(placed_employee_ids_query)


relationships = PeopleRelationships()

__all__ = [
    "PeopleRelationships",
    "first_name",
    "has_direct_reports",
    "is_employed",
    "is_team_member",
    "placed_employee_ids_query",
    "placement",
    "relationships",
    "team_member_ids",
    "team_member_ids_query",
    "work_email",
]
