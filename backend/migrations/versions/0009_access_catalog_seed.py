"""Seed the permission catalog and the system roles (docs/authorization-model.md §3-4).

Revision ID: 0009
Revises: 0008

Data revision. The values below are a frozen copy of `app/platform/authz/catalog.py` and
`roles.py` at the time of writing: revisions are history and never import application code.
A test compares the database with the code catalog, so they cannot drift; a later change
to the catalog or the matrix is a new data revision.

The matrix is the proposed default that Sigvitas HR confirms before M2 (roadmap §6). No
user is created and no role is assigned here: the first super admins come from the
bootstrap command (app/cli.py).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS: tuple[tuple[str, str, bool, bool], ...] = (
    ("auth.session.read.self", "See own sessions and login history", False, False),
    ("auth.session.revoke.self", "Revoke own sessions", False, False),
    ("auth.session.read.all", "See sessions of all users", False, True),
    ("auth.session.revoke.all", "Revoke any user's sessions", True, True),
    ("user.read.all", "List user accounts and account state", False, False),
    ("user.invite", "Create accounts and resend invites for employees", False, True),
    ("user.disable", "Disable and re-enable accounts", True, True),
    ("security.mfa.reset", "Reset another user's MFA", True, True),
    ("security.event.read", "Read security events and login history of all users", False, True),
    ("security.settings.manage", "Password, session, lockout and IP policies", True, True),
    ("role.read", "View roles and their permissions, and who holds them", False, False),
    ("role.assign", "Assign and remove roles for other users", True, True),
    ("role.elevation.request", "Request a time-bound role for oneself", True, True),
    ("role.elevation.approve", "Approve or reject another super admin's elevation request", True, True),
    ("role.elevation.break_glass", "Self-activate system_admin for up to 1 hour", True, True),
    ("audit.read", "Read the audit log", False, True),
    ("audit.export", "Export the audit log", True, True),
    ("settings.read", "Read system settings", False, False),
    ("settings.manage", "Change system settings", True, True),
    ("org.read", "Departments, designations, locations", False, False),
    ("org.manage", "Create, update and archive departments, designations, locations", False, True),
    ("employee.directory.read", "Directory fields of active employees", False, False),
    (
        "employee.profile.read.self",
        "Own job details, reporting line, joining date, employment type, status",
        False,
        False,
    ),
    (
        "employee.profile.read.team",
        "Team members' job details, reporting line, joining date, status",
        False,
        False,
    ),
    (
        "employee.profile.read.all",
        "All employees' job details, reporting line, joining date, status",
        False,
        False,
    ),
    ("employee.profile.update.self", "Own self-editable profile fields", False, False),
    ("employee.profile.update.all", "HR edits of profile fields", False, True),
    (
        "employee.personal.read.self",
        "Own date of birth, personal contact, address, emergency contacts",
        False,
        False,
    ),
    ("employee.personal.read.all", "Others' personal details", False, True),
    ("employee.personal.update.self", "Own personal details", False, False),
    ("employee.personal.update.all", "HR edits of personal details", False, True),
    ("employee.emergency_contact.read.team", "Emergency contacts of team members", False, True),
    (
        "employee.sensitive.read.self",
        "Own government IDs and bank details (unmasked on step-up)",
        False,
        False,
    ),
    ("employee.sensitive.read.all", "Others' sensitive identifiers", True, True),
    ("employee.sensitive.update.all", "Edit sensitive identifiers", True, True),
    ("employee.create", "Create employee records", False, True),
    ("employee.lifecycle.manage", "Job changes, status changes and exits", False, True),
    ("employee.export", "Export employee data", True, True),
    ("attendance.clock.self", "Clock in and out, start and end breaks", False, False),
    ("attendance.read.self", "Own attendance events and daily summaries", False, False),
    ("attendance.read.team", "Team attendance events and daily summaries", False, False),
    ("attendance.read.all", "All attendance events and daily summaries", False, False),
    ("attendance.correction.request.self", "Submit a correction for own attendance", False, False),
    ("attendance.correction.approve.team", "Decide corrections for the team", False, False),
    ("attendance.correction.approve.all", "Decide any correction, including locked periods", False, True),
    ("attendance.correction.create.all", "HR-initiated correction on behalf of an employee", False, True),
    ("attendance.export", "Export attendance", True, True),
    ("shift.read", "View shift definitions", False, False),
    ("shift.manage", "Define shifts", False, True),
    ("attendance.policy.manage", "Define effective-dated attendance policies", False, True),
    ("shift.assign.all", "Assign shifts to employees", False, True),
    ("leave.request.self", "Apply for and cancel own leave", False, False),
    ("leave.read.self", "Own leave requests and balances", False, False),
    ("leave.read.team", "Team leave requests and balances", False, False),
    ("leave.read.all", "All leave requests and balances", False, False),
    ("leave.request.approve.team", "Decide team leave requests", False, False),
    ("leave.request.approve.all", "Decide any leave request", False, True),
    ("leave.balance.adjust", "Manual leave ledger adjustments with a reason", False, True),
    ("leave.policy.manage", "Leave types and accrual rules", False, True),
    ("leave.export", "Export leave data", True, True),
    ("holiday.read", "View holiday calendars", False, False),
    ("holiday.manage", "Maintain holiday calendars", False, True),
    ("document.read.self", "Own documents in employee-visible categories", False, True),
    ("document.upload.self", "Upload own documents to self-upload categories", False, False),
    ("document.read.team", "Team documents in internal categories", False, True),
    ("document.read.all", "All documents in internal and personal categories", False, True),
    ("document.sensitive.read.all", "Documents in sensitive categories", True, True),
    ("document.manage.all", "Upload, replace and archive documents for any employee", False, True),
    ("document.category.manage", "Define document categories and their sensitivity", False, True),
    ("document.company.read", "Company-wide documents", False, False),
    ("document.company.manage", "Publish company-wide documents", False, True),
    ("notification.read.self", "Own notifications", False, False),
    ("notification.preference.update.self", "Own notification preferences", False, False),
)

ROLES: tuple[tuple[str, str, str, bool, tuple[str, ...]], ...] = (
    (
        "employee",
        "Employee",
        "Self-service for the linked employee record",
        True,
        (
            "attendance.clock.self",
            "attendance.correction.request.self",
            "attendance.read.self",
            "document.company.read",
            "document.read.self",
            "document.upload.self",
            "employee.directory.read",
            "employee.personal.read.self",
            "employee.personal.update.self",
            "employee.profile.read.self",
            "employee.profile.update.self",
            "employee.sensitive.read.self",
            "holiday.read",
            "leave.read.self",
            "leave.request.self",
            "notification.preference.update.self",
            "notification.read.self",
            "org.read",
            "shift.read",
        ),
    ),
    (
        "manager",
        "Manager",
        "Team scope while the employee has direct reports",
        True,
        (
            "attendance.correction.approve.team",
            "attendance.read.team",
            "document.read.team",
            "employee.profile.read.team",
            "leave.read.team",
            "leave.request.approve.team",
        ),
    ),
    (
        "hr",
        "HR",
        "Day-to-day HR operations",
        False,
        (
            "attendance.correction.approve.all",
            "attendance.correction.create.all",
            "attendance.export",
            "attendance.read.all",
            "document.company.manage",
            "document.manage.all",
            "document.read.all",
            "employee.create",
            "employee.lifecycle.manage",
            "employee.personal.read.all",
            "employee.personal.update.all",
            "employee.profile.read.all",
            "employee.profile.update.all",
            "leave.balance.adjust",
            "leave.export",
            "leave.read.all",
            "leave.request.approve.all",
            "shift.assign.all",
            "user.disable",
            "user.invite",
            "user.read.all",
        ),
    ),
    (
        "hr_admin",
        "HR admin",
        "HR configuration and policies",
        False,
        (
            "attendance.correction.approve.all",
            "attendance.correction.create.all",
            "attendance.export",
            "attendance.policy.manage",
            "attendance.read.all",
            "document.category.manage",
            "document.company.manage",
            "document.manage.all",
            "document.read.all",
            "document.sensitive.read.all",
            "employee.create",
            "employee.export",
            "employee.lifecycle.manage",
            "employee.personal.read.all",
            "employee.personal.update.all",
            "employee.profile.read.all",
            "employee.profile.update.all",
            "employee.sensitive.read.all",
            "employee.sensitive.update.all",
            "holiday.manage",
            "leave.balance.adjust",
            "leave.export",
            "leave.policy.manage",
            "leave.read.all",
            "leave.request.approve.all",
            "org.manage",
            "role.read",
            "settings.read",
            "shift.assign.all",
            "shift.manage",
            "user.disable",
            "user.invite",
            "user.read.all",
        ),
    ),
    (
        "payroll_admin",
        "Payroll admin",
        "Compensation and payroll",
        False,
        (
            "attendance.export",
            "attendance.read.all",
            "employee.profile.read.all",
            "leave.export",
            "leave.read.all",
        ),
    ),
    (
        "system_admin",
        "System admin",
        "Accounts, sessions, settings, security events",
        False,
        (
            "auth.session.read.all",
            "auth.session.revoke.all",
            "role.read",
            "security.event.read",
            "security.mfa.reset",
            "security.settings.manage",
            "settings.manage",
            "settings.read",
            "user.disable",
            "user.invite",
            "user.read.all",
        ),
    ),
    (
        "super_admin",
        "Super admin",
        "Role and permission management, controlled elevation, break-glass recovery; no data access",
        False,
        (
            "audit.read",
            "role.assign",
            "role.elevation.approve",
            "role.elevation.break_glass",
            "role.elevation.request",
            "role.read",
            "security.event.read",
            "settings.read",
            "user.read.all",
        ),
    ),
    (
        "auditor",
        "Auditor",
        "Read-only audit and security events",
        False,
        (
            "audit.export",
            "audit.read",
            "auth.session.read.all",
            "role.read",
            "security.event.read",
        ),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    for key, description, step_up, audit_reads in PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO access.permissions (key, description, requires_step_up, audit_reads) "
                "VALUES (:key, :description, :step_up, :audit_reads)"
            ),
            {"key": key, "description": description, "step_up": step_up, "audit_reads": audit_reads},
        )
    for key, name, description, derived, permissions in ROLES:
        bind.execute(
            sa.text(
                "INSERT INTO access.roles (key, name, description, is_system, is_derived) "
                "VALUES (:key, :name, :description, true, :derived)"
            ),
            {"key": key, "name": name, "description": description, "derived": derived},
        )
        bind.execute(
            sa.text(
                "INSERT INTO access.role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id FROM access.roles r "
                "JOIN access.permissions p ON p.key = ANY(:permissions) WHERE r.key = :key"
            ),
            {"key": key, "permissions": list(permissions)},
        )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DELETE FROM access.role_permissions")
    op.execute("DELETE FROM access.roles")
    op.execute("DELETE FROM access.permissions")
