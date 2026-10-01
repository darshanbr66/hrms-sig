"""The people module's interface for other modules (docs/architecture.md §4).

Team resolution for the authorization engine (docs/authorization-model.md §7):

- `team_member_ids_query` is a SELECT of employee IDs, for embedding in a list query's scope
  filter as `employee_id IN (...)` without materializing the team first.
- `is_team_member` answers the single-record question by walking up the reporting chain.
- `has_direct_reports` decides the derived `manager` role.

Every function takes the calendar date to resolve reporting lines on. "Now" is the current
date where the caller decides it; a historical record passes the record's own date, which
gives the "team at the time of the record" rule.
"""

from app.modules.people.repository import (
    has_direct_reports,
    is_team_member,
    team_member_ids,
    team_member_ids_query,
)

__all__ = ["has_direct_reports", "is_team_member", "team_member_ids", "team_member_ids_query"]
