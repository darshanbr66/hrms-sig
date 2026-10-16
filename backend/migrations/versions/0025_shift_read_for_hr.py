"""`shift.read` for `hr` and `hr_admin` (docs/authorization-model.md §4.1).

Revision ID: 0025
Revises: 0024

Data only. HR assigns shifts (`shift.assign.all`) and HR admins define shifts and attendance
policies (`shift.manage`, `attendance.policy.manage`), which needs the shifts and policies to
choose from and review, and an HR account is not always linked to an employee record (whose
derived `employee` role is the only one holding `shift.read` so far). The same reasoning as
`org.read` and `holiday.read` in revision 0014. Shift definitions hold no personal data.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_GRANTS = (("hr", "shift.read"), ("hr_admin", "shift.read"))


def upgrade() -> None:
    for role, permission in ROLE_GRANTS:
        op.execute(
            "INSERT INTO access.role_permissions (role_id, permission_id) "
            f"SELECT r.id, p.id FROM access.roles r, access.permissions p "
            f"WHERE r.key = '{role}' AND p.key = '{permission}'"
        )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    for role, permission in ROLE_GRANTS:
        op.execute(
            "DELETE FROM access.role_permissions WHERE "
            f"role_id = (SELECT id FROM access.roles WHERE key = '{role}') AND "
            f"permission_id = (SELECT id FROM access.permissions WHERE key = '{permission}')"
        )
