"""Permission catalog: the source of truth for permission keys (docs/authorization-model.md §3).

Keys follow `<domain>.<resource>.<action>[.<scope>]`; the resource is omitted when the domain
has one main resource (`user.invite`, `org.read`), so a key has two to four parts. Flags:
`step_up` (SU) needs MFA re-verification within the step-up window; `audit_reads` (R) means
every use is audited, reads included.

Only MVP permissions are listed. Phase 2 permissions (custom roles, compensation, payroll,
the department leave calendar) are added with the features that use them. The database copy
(`access.permissions`, revision 0009) is checked against this catalog by a test.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

PERMISSION_KEY: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}")


class Scope(StrEnum):
    SELF = "self"
    TEAM = "team"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class Permission:
    key: str
    description: str
    step_up: bool = False
    audit_reads: bool = False

    def __post_init__(self) -> None:
        if not PERMISSION_KEY.fullmatch(self.key):
            raise ValueError(f"invalid permission key {self.key!r}")

    @property
    def scope(self) -> Scope | None:
        last = self.key.rsplit(".", 1)[-1]
        return Scope(last) if last in Scope.__members__.values() else None

    @property
    def base(self) -> str:
        """The key without its scope: `leave.read.team` -> `leave.read`."""
        return self.key.rsplit(".", 1)[0] if self.scope is not None else self.key


def _p(key: str, description: str, *, su: bool = False, r: bool = False) -> Permission:
    return Permission(key, description, step_up=su, audit_reads=r)


CATALOG: Final[tuple[Permission, ...]] = (
    # 3.1 Identity, access, security
    _p("auth.session.read.self", "See own sessions and login history"),
    _p("auth.session.revoke.self", "Revoke own sessions"),
    _p("auth.session.read.all", "See sessions of all users", r=True),
    _p("auth.session.revoke.all", "Revoke any user's sessions", su=True, r=True),
    _p("user.read.all", "List user accounts and account state"),
    _p("user.invite", "Create accounts and resend invites for employees", r=True),
    _p("user.disable", "Disable and re-enable accounts", su=True, r=True),
    _p("security.mfa.reset", "Reset another user's MFA", su=True, r=True),
    _p("security.event.read", "Read security events and login history of all users", r=True),
    _p("security.settings.manage", "Password, session, lockout and IP policies", su=True, r=True),
    _p("role.read", "View roles and their permissions, and who holds them"),
    _p("role.assign", "Assign and remove roles for other users", su=True, r=True),
    _p("role.elevation.request", "Request a time-bound role for oneself", su=True, r=True),
    _p(
        "role.elevation.approve", "Approve or reject another super admin's elevation request", su=True, r=True
    ),
    _p("role.elevation.break_glass", "Self-activate system_admin for up to 1 hour", su=True, r=True),
    _p("audit.read", "Read the audit log", r=True),
    _p("audit.export", "Export the audit log", su=True, r=True),
    _p("settings.read", "Read system settings"),
    _p("settings.manage", "Change system settings", su=True, r=True),
    # 3.2 Organization and people
    _p("org.read", "Departments, designations, locations"),
    _p("org.manage", "Create, update and archive departments, designations, locations", r=True),
    _p("employee.directory.read", "Directory fields of active employees"),
    _p(
        "employee.profile.read.self", "Own job details, reporting line, joining date, employment type, status"
    ),
    _p("employee.profile.read.team", "Team members' job details, reporting line, joining date, status"),
    _p("employee.profile.read.all", "All employees' job details, reporting line, joining date, status"),
    _p("employee.profile.update.self", "Own self-editable profile fields"),
    _p("employee.profile.update.all", "HR edits of profile fields", r=True),
    _p("employee.personal.read.self", "Own date of birth, personal contact, address, emergency contacts"),
    _p("employee.personal.read.all", "Others' personal details", r=True),
    _p("employee.personal.update.self", "Own personal details"),
    _p("employee.personal.update.all", "HR edits of personal details", r=True),
    _p("employee.emergency_contact.read.team", "Emergency contacts of team members", r=True),
    _p("employee.sensitive.read.self", "Own government IDs and bank details (unmasked on step-up)"),
    _p("employee.sensitive.read.all", "Others' sensitive identifiers", su=True, r=True),
    _p("employee.sensitive.update.all", "Edit sensitive identifiers", su=True, r=True),
    _p("employee.create", "Create employee records", r=True),
    _p("employee.lifecycle.manage", "Job changes, status changes and exits", r=True),
    _p("employee.export", "Export employee data", su=True, r=True),
    # 3.3 Attendance and shifts
    _p("attendance.clock.self", "Clock in and out, start and end breaks"),
    _p("attendance.read.self", "Own attendance events and daily summaries"),
    _p("attendance.read.team", "Team attendance events and daily summaries"),
    _p("attendance.read.all", "All attendance events and daily summaries"),
    _p("attendance.correction.request.self", "Submit a correction for own attendance"),
    _p("attendance.correction.approve.team", "Decide corrections for the team"),
    _p("attendance.correction.approve.all", "Decide any correction, including locked periods", r=True),
    _p("attendance.correction.create.all", "HR-initiated correction on behalf of an employee", r=True),
    _p("attendance.export", "Export attendance", su=True, r=True),
    _p("shift.read", "View shift definitions"),
    _p("shift.manage", "Define shifts", r=True),
    _p("attendance.policy.manage", "Define effective-dated attendance policies", r=True),
    _p("shift.assign.all", "Assign shifts to employees", r=True),
    # 3.4 Leave and holidays
    _p("leave.request.self", "Apply for and cancel own leave"),
    _p("leave.read.self", "Own leave requests and balances"),
    _p("leave.read.team", "Team leave requests and balances"),
    _p("leave.read.all", "All leave requests and balances"),
    _p("leave.request.approve.team", "Decide team leave requests"),
    _p("leave.request.approve.all", "Decide any leave request", r=True),
    _p("leave.balance.adjust", "Manual leave ledger adjustments with a reason", r=True),
    _p("leave.policy.manage", "Leave types and accrual rules", r=True),
    _p("leave.export", "Export leave data", su=True, r=True),
    _p("holiday.read", "View holiday calendars"),
    _p("holiday.manage", "Maintain holiday calendars", r=True),
    # 3.6 Documents
    _p("document.read.self", "Own documents in employee-visible categories", r=True),
    _p("document.upload.self", "Upload own documents to self-upload categories"),
    _p("document.read.team", "Team documents in internal categories", r=True),
    _p("document.read.all", "All documents in internal and personal categories", r=True),
    _p("document.sensitive.read.all", "Documents in sensitive categories", su=True, r=True),
    _p("document.manage.all", "Upload, replace and archive documents for any employee", r=True),
    _p("document.category.manage", "Define document categories and their sensitivity", r=True),
    _p("document.company.read", "Company-wide documents"),
    _p("document.company.manage", "Publish company-wide documents", r=True),
    # 3.7 Notifications (MVP)
    _p("notification.read.self", "Own notifications"),
    _p("notification.preference.update.self", "Own notification preferences"),
)

PERMISSIONS: Final[dict[str, Permission]] = {permission.key: permission for permission in CATALOG}
if len(PERMISSIONS) != len(CATALOG):
    raise RuntimeError("duplicate permission key in the catalog")


def scoped_keys(base: str) -> tuple[str, ...]:
    """Catalog keys for a permission base at each scope, widest first."""
    return tuple(
        f"{base}.{scope}" for scope in (Scope.ALL, Scope.TEAM, Scope.SELF) if f"{base}.{scope}" in PERMISSIONS
    )
