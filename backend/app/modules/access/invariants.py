"""An assignment matches the grant it comes from (CLAUDE.md rule 16, SOD-8; formerly the
`check_assignment_grant` trigger).

- `super_admin` is assigned only through an approved super admin assignment request, or by the
  installation bootstrap (no `granted_by`).
- An assignment from a request names the request's person, role and constraint, and:
  a super admin assignment is approved, has no end and was granted by its decider; an
  elevation is active, runs exactly the request's window and was granted by its decider; a
  break-glass is active, runs exactly its window and was granted by the person themselves.

Every new assignment registers `require_grant_match`, which checks it before the unit of work
commits; a mismatch aborts the transaction.
"""

from app.modules.access.models import GrantKind, GrantStatus, RoleGrantRequest, UserRole
from app.platform.authz.roles import RoleKey
from app.platform.db import Session


class AssignmentGrantError(RuntimeError):
    """An assignment does not match the grant it comes from."""


def require_grant_match(session: Session, assignment: UserRole) -> None:
    async def check(current: Session) -> None:
        await _check(current, assignment)

    session.check_before_commit(f"access.assignment:{assignment.id}", check)


async def _check(session: Session, assignment: UserRole) -> None:
    if assignment.grant_request_id is None:
        if assignment.role_key == RoleKey.SUPER_ADMIN and assignment.granted_by is not None:
            raise AssignmentGrantError("super_admin is assigned only through an approved request (SOD-8)")
        return
    request = await session.get(RoleGrantRequest, assignment.grant_request_id)
    if request is None or (
        request.subject_user_id,
        request.role_key,
        request.department_id,
        request.location_id,
    ) != (assignment.user_id, assignment.role_key, assignment.department_id, assignment.location_id):
        raise AssignmentGrantError("an assignment must match the grant request it comes from")
    allowed = (
        (
            request.kind == GrantKind.SUPER_ADMIN_ASSIGNMENT
            and request.status == GrantStatus.APPROVED
            and assignment.valid_until is None
            and assignment.granted_by == request.decided_by
        )
        or (
            request.kind == GrantKind.ELEVATION
            and request.status == GrantStatus.ACTIVE
            and assignment.valid_from == request.starts_at
            and assignment.valid_until == request.ends_at
            and assignment.granted_by == request.decided_by
        )
        or (
            request.kind == GrantKind.BREAK_GLASS
            and request.status == GrantStatus.ACTIVE
            and assignment.valid_from == request.starts_at
            and assignment.valid_until == request.ends_at
            and assignment.granted_by == assignment.user_id
        )
    )
    if not allowed:
        raise AssignmentGrantError("the grant request does not allow this assignment")
