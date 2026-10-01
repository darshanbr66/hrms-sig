"""System roles and their permissions (docs/authorization-model.md §4.1).

The matrix is the proposed default Sigvitas HR confirms before M2 (roadmap §6); changing it
is a data migration. Least-privilege boundaries that are fixed architecture, and tested:
`system_admin` and `super_admin` hold no employee data, document or compensation access;
`manager` holds no personal or sensitive access to team members.

`employee` and `manager` are derived, never assigned: `employee` while the account is linked
to an employee record who is employed on the day, `manager` while that employee has at least
one direct report on the day. Every active account also holds the account baseline (its own
sessions and login history), whether or not it is linked to an employee record, because
accounts such as the bootstrapped super admins have no employee record.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.platform.authz.catalog import PERMISSIONS


class RoleKey(StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR = "hr"
    HR_ADMIN = "hr_admin"
    PAYROLL_ADMIN = "payroll_admin"
    SYSTEM_ADMIN = "system_admin"
    SUPER_ADMIN = "super_admin"
    AUDITOR = "auditor"


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    key: RoleKey
    name: str
    description: str
    derived: bool
    permissions: frozenset[str]

    def __post_init__(self) -> None:
        unknown = self.permissions - PERMISSIONS.keys()
        if unknown:
            raise ValueError(f"role {self.key} names unknown permissions: {sorted(unknown)}")


ACCOUNT_BASELINE: Final = frozenset({"auth.session.read.self", "auth.session.revoke.self"})

_EMPLOYEE: Final = frozenset(
    {
        "org.read",
        "employee.directory.read",
        "employee.profile.read.self",
        "employee.profile.update.self",
        "employee.personal.read.self",
        "employee.personal.update.self",
        "employee.sensitive.read.self",
        "attendance.clock.self",
        "attendance.read.self",
        "attendance.correction.request.self",
        "shift.read",
        "leave.request.self",
        "leave.read.self",
        "holiday.read",
        "document.read.self",
        "document.upload.self",
        "document.company.read",
        "notification.read.self",
        "notification.preference.update.self",
    }
)

_MANAGER: Final = frozenset(
    {
        "employee.profile.read.team",
        "attendance.read.team",
        "attendance.correction.approve.team",
        "leave.read.team",
        "leave.request.approve.team",
        "document.read.team",
    }
)

_HR: Final = frozenset(
    {
        "user.read.all",
        "user.invite",
        "user.disable",
        "employee.profile.read.all",
        "employee.profile.update.all",
        "employee.personal.read.all",
        "employee.personal.update.all",
        "employee.create",
        "employee.lifecycle.manage",
        "attendance.read.all",
        "attendance.correction.approve.all",
        "attendance.correction.create.all",
        "attendance.export",
        "shift.assign.all",
        "leave.read.all",
        "leave.request.approve.all",
        "leave.balance.adjust",
        "leave.export",
        "document.read.all",
        "document.manage.all",
        "document.company.manage",
    }
)

_HR_ADMIN: Final = _HR | frozenset(
    {
        "role.read",
        "settings.read",
        "org.manage",
        "employee.sensitive.read.all",
        "employee.sensitive.update.all",
        "employee.export",
        "shift.manage",
        "attendance.policy.manage",
        "leave.policy.manage",
        "holiday.manage",
        "document.sensitive.read.all",
        "document.category.manage",
    }
)

# MVP grants only; compensation, payroll and bank details arrive with Phase 2.
_PAYROLL_ADMIN: Final = frozenset(
    {
        "employee.profile.read.all",
        "attendance.read.all",
        "attendance.export",
        "leave.read.all",
        "leave.export",
    }
)

_SYSTEM_ADMIN: Final = frozenset(
    {
        "auth.session.read.all",
        "auth.session.revoke.all",
        "user.read.all",
        "user.invite",
        "user.disable",
        "security.mfa.reset",
        "security.event.read",
        "security.settings.manage",
        "role.read",
        "settings.read",
        "settings.manage",
    }
)

_SUPER_ADMIN: Final = frozenset(
    {
        "user.read.all",
        "security.event.read",
        "role.read",
        "role.assign",
        "role.elevation.request",
        "role.elevation.approve",
        "role.elevation.break_glass",
        "audit.read",
        "settings.read",
    }
)

_AUDITOR: Final = frozenset(
    {"auth.session.read.all", "security.event.read", "role.read", "audit.read", "audit.export"}
)

ROLES: Final[tuple[RoleDefinition, ...]] = (
    RoleDefinition(
        RoleKey.EMPLOYEE, "Employee", "Self-service for the linked employee record", True, _EMPLOYEE
    ),
    RoleDefinition(
        RoleKey.MANAGER, "Manager", "Team scope while the employee has direct reports", True, _MANAGER
    ),
    RoleDefinition(RoleKey.HR, "HR", "Day-to-day HR operations", False, _HR),
    RoleDefinition(RoleKey.HR_ADMIN, "HR admin", "HR configuration and policies", False, _HR_ADMIN),
    RoleDefinition(RoleKey.PAYROLL_ADMIN, "Payroll admin", "Compensation and payroll", False, _PAYROLL_ADMIN),
    RoleDefinition(
        RoleKey.SYSTEM_ADMIN,
        "System admin",
        "Accounts, sessions, settings, security events",
        False,
        _SYSTEM_ADMIN,
    ),
    RoleDefinition(
        RoleKey.SUPER_ADMIN,
        "Super admin",
        "Role and permission management, controlled elevation, break-glass recovery; no data access",
        False,
        _SUPER_ADMIN,
    ),
    RoleDefinition(RoleKey.AUDITOR, "Auditor", "Read-only audit and security events", False, _AUDITOR),
)

ROLE_DEFINITIONS: Final[dict[RoleKey, RoleDefinition]] = {role.key: role for role in ROLES}

# SOD-10: a super admin holds none of these as standing (non-elevated) assignments.
DATA_ROLES: Final = frozenset(
    {RoleKey.HR, RoleKey.HR_ADMIN, RoleKey.PAYROLL_ADMIN, RoleKey.SYSTEM_ADMIN, RoleKey.AUDITOR}
)

# docs/security-architecture.md §9: there must be at least two super admins.
MIN_SUPER_ADMINS: Final = 2
