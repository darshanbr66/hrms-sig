"""The authorization matrix over HTTP (docs/authorization-model.md §4.1, §8; security-architecture §12).

For every system role and every permission-gated route, the expected outcome is computed from
the role matrix in code: a role holding the route's permission gets through (or is asked to
step up, for SU permissions); every other role gets 403. Unauthenticated requests get 401.
Allowed and denied cases are both asserted, so a route that is too open or too closed fails.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest

from app.modules.access.models import UserRole, role_id
from app.platform.audit.tables import AuditLogRecord
from app.platform.authz.catalog import PERMISSIONS
from app.platform.authz.engine import Authorizer
from app.platform.authz.roles import ACCOUNT_BASELINE, ROLE_DEFINITIONS, RoleKey
from tests.api_support import Account, Api
from tests.people_data import add_job, create_employee, create_org_units


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    permission: str
    body: dict[str, object] | None = None
    # What "allowed" looks like when the path names something that does not exist.
    allowed_statuses: frozenset[int] = frozenset({200, 201, 204})
    params: dict[str, str] | None = None
    headers: dict[str, str] | None = None


# {target} is another account, {assignment} one of its role assignments, {role} a role and
# {session} a session that does not exist. Every permission-gated route of this checkpoint is
# listed; tests/security/test_route_coverage.py checks the list is complete.
ROUTES = (
    Route("GET", "/api/v1/users", "user.read.all"),
    Route("GET", "/api/v1/users/{target}", "user.read.all"),
    Route("POST", "/api/v1/users/{target}/disable", "user.disable"),
    Route("POST", "/api/v1/users/{target}/enable", "user.disable"),
    Route("GET", "/api/v1/users/{target}/sessions", "auth.session.read.all"),
    Route("DELETE", "/api/v1/users/{target}/sessions", "auth.session.revoke.all"),
    Route("GET", "/api/v1/roles", "role.read"),
    Route("GET", "/api/v1/roles/{role}", "role.read"),
    Route("GET", "/api/v1/users/{target}/roles", "role.read"),
    Route(
        "POST",
        "/api/v1/users/{target}/roles",
        "role.assign",
        {"role_key": "hr", "reason": "Matrix test"},
    ),
    Route(
        "POST",
        "/api/v1/users/{target}/roles/batch",
        "role.assign",
        {"roles": [{"role_key": "payroll_admin"}], "reason": "Matrix test"},
    ),
    Route("DELETE", "/api/v1/users/{target}/roles/{assignment}", "role.assign"),
    Route(
        "POST",
        "/api/v1/users",
        "user.invite",
        {"email": "matrix-invite@dev.example"},
        # The first allowed role creates the account; later ones find the email in use.
        allowed_statuses=frozenset({201, 409}),
    ),
    # The target is an active account, so an allowed caller is told it is not waiting for an invite.
    Route("POST", "/api/v1/users/{target}/invite", "user.invite", allowed_statuses=frozenset({409})),
    Route("DELETE", "/api/v1/users/{target}/invite", "user.invite", allowed_statuses=frozenset({409})),
    Route("GET", "/api/v1/settings", "settings.read"),
    Route("PUT", "/api/v1/settings/email.delivery.max_attempts", "settings.manage", {"value": None}),
    Route("GET", "/api/v1/me/devices", "auth.session.read.self"),
    Route(
        "DELETE",
        "/api/v1/me/devices/{device}",
        "auth.session.revoke.self",
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/audit-log", "audit.read"),
    Route("GET", "/api/v1/security-events", "security.event.read"),
    Route(
        "POST",
        "/api/v1/users/{target}/mfa-reset",
        "security.mfa.reset",
        {"reason": "Matrix test"},
        allowed_statuses=frozenset({202}),
    ),
    Route("GET", "/api/v1/role-grant-requests", "role.read"),
    Route(
        "POST",
        "/api/v1/role-grant-requests",
        "role.elevation.request",
        {"role_key": "auditor", "reason": "Matrix test"},
        allowed_statuses=frozenset({201}),
    ),
    # {grant_request} does not exist: an allowed caller learns only that.
    Route(
        "POST",
        "/api/v1/role-grant-requests/{grant_request}/approve",
        "role.elevation.approve",
        {},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/role-grant-requests/{grant_request}/reject",
        "role.elevation.approve",
        {},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/role-grant-requests/{grant_request}/revoke",
        "role.elevation.approve",
        allowed_statuses=frozenset({404}),
    ),
    # Permanent erasure: super admins only; {erasure} does not exist, and the code is unknown.
    Route("GET", "/api/v1/employee-erasures", "employee.erasure.read"),
    Route(
        "GET",
        "/api/v1/employee-erasures/{erasure}",
        "employee.erasure.read",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/employee-erasures",
        "employee.erasure.request",
        {"employee_code": "MATRIX-NONE", "reason": "Matrix test"},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/employee-erasures/{erasure}/approve",
        "employee.erasure.approve",
        {"employee_code": "MATRIX-NONE"},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/employee-erasures/{erasure}/reject",
        "employee.erasure.approve",
        {"note": "Matrix test"},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/employee-erasures/{erasure}/cancel",
        "employee.erasure.request",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/role-grant-requests/{grant_request}/acknowledge",
        "role.elevation.approve",
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/me/sessions", "auth.session.read.self"),
    Route("GET", "/api/v1/me/login-history", "auth.session.read.self"),
    Route("DELETE", "/api/v1/me/sessions", "auth.session.revoke.self"),
    Route(
        "DELETE",
        "/api/v1/me/sessions/{session}",
        "auth.session.revoke.self",
        allowed_statuses=frozenset({404}),
    ),
    # Organization. {location}, {department} and {designation} do not exist; a PATCH without
    # If-Match is refused as a validation error once the permission check has passed.
    Route("GET", "/api/v1/locations", "org.read"),
    Route("GET", "/api/v1/locations/{location}", "org.read", allowed_statuses=frozenset({404})),
    Route(
        "POST",
        "/api/v1/locations",
        "org.manage",
        {"code": "MATRIX-LOC", "name": "Test location", "time_zone": "Asia/Kolkata", "country_code": "IN"},
        allowed_statuses=frozenset({201, 409}),
    ),
    Route("PATCH", "/api/v1/locations/{location}", "org.manage", {}, allowed_statuses=frozenset({422})),
    Route("GET", "/api/v1/departments", "org.read"),
    Route("GET", "/api/v1/departments/{department}", "org.read", allowed_statuses=frozenset({404})),
    Route(
        "POST",
        "/api/v1/departments",
        "org.manage",
        {"code": "MATRIX-DEP", "name": "Test department"},
        allowed_statuses=frozenset({201, 409}),
    ),
    Route("PATCH", "/api/v1/departments/{department}", "org.manage", {}, allowed_statuses=frozenset({422})),
    Route("GET", "/api/v1/designations", "org.read"),
    Route("GET", "/api/v1/designations/{designation}", "org.read", allowed_statuses=frozenset({404})),
    Route(
        "POST",
        "/api/v1/designations",
        "org.manage",
        {"code": "MATRIX-DES", "name": "Test designation"},
        allowed_statuses=frozenset({201, 409}),
    ),
    Route("PATCH", "/api/v1/designations/{designation}", "org.manage", {}, allowed_statuses=frozenset({422})),
    # People. {employee} does not exist: it is outside every self or team scope (404), and an
    # `all` holder finds nothing (404), or an empty history (200).
    Route("GET", "/api/v1/directory", "employee.directory.read"),
    Route("GET", "/api/v1/employees", "employee.profile.read"),
    Route("GET", "/api/v1/former-employees", "employee.profile.read"),
    Route("GET", "/api/v1/employees/statistics", "employee.profile.read"),
    Route(
        "POST",
        "/api/v1/employees",
        "employee.create",
        {
            "employee_code": "MATRIX-EMP",
            "legal_first_name": "Test",
            "date_of_joining": "2026-01-05",
            "job": {
                "department_id": str(uuid.UUID(int=1)),
                "designation_id": str(uuid.UUID(int=2)),
                "location_id": str(uuid.UUID(int=3)),
                "employment_type": "full_time",
            },
        },
        # The units do not exist.
        allowed_statuses=frozenset({422}),
    ),
    Route("GET", "/api/v1/employees/{employee}", "employee.profile.read", allowed_statuses=frozenset({404})),
    Route(
        "PATCH",
        "/api/v1/employees/{employee}",
        "employee.profile.update.all",
        {},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "GET",
        "/api/v1/employees/{employee}/jobs",
        "employee.profile.read",
        allowed_statuses=frozenset({200, 404}),
    ),
    Route(
        "POST",
        "/api/v1/employees/{employee}/jobs",
        "employee.lifecycle.manage",
        {
            "effective_from": "2026-02-01",
            "department_id": str(uuid.UUID(int=1)),
            "designation_id": str(uuid.UUID(int=2)),
            "location_id": str(uuid.UUID(int=3)),
            "employment_type": "full_time",
            "change_reason": "transfer",
        },
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "GET",
        "/api/v1/employees/{employee}/status-changes",
        "employee.profile.read",
        allowed_statuses=frozenset({200, 404}),
    ),
    Route(
        "POST",
        "/api/v1/employees/{employee}/status-changes",
        "employee.lifecycle.manage",
        {"to_status": "on_notice", "effective_date": "2026-02-01", "reason": "Matrix test"},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "GET",
        "/api/v1/employees/{employee}/personal",
        "employee.personal.read",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "PATCH",
        "/api/v1/employees/{employee}/personal",
        "employee.personal.update.all",
        {},
        allowed_statuses=frozenset({422}),
    ),
    Route("GET", "/api/v1/me/profile", "employee.profile.read.self"),
    Route(
        "PATCH", "/api/v1/me/profile", "employee.profile.update.self", {}, allowed_statuses=frozenset({422})
    ),
    Route("GET", "/api/v1/me/jobs", "employee.profile.read.self"),
    Route("GET", "/api/v1/me/personal", "employee.personal.read.self"),
    Route(
        "PATCH", "/api/v1/me/personal", "employee.personal.update.self", {}, allowed_statuses=frozenset({422})
    ),
    # Attendance. Matrix employees have a current job, so clocking in works.
    Route("GET", "/api/v1/me/attendance/today", "attendance.read.self"),
    Route(
        "POST",
        "/api/v1/me/attendance/events",
        "attendance.clock.self",
        {"event_type": "CLOCK_IN"},
        headers={"Idempotency-Key": "matrix-clock-in"},
    ),
    Route(
        "GET",
        "/api/v1/me/attendance/days",
        "attendance.read.self",
        params={"from": "2026-01-01", "to": "2026-01-31"},
    ),
    Route(
        "GET",
        "/api/v1/employees/{employee}/attendance/days",
        "attendance.read",
        params={"from": "2026-01-01", "to": "2026-01-31"},
        allowed_statuses=frozenset({200, 404}),
    ),
    Route("GET", "/api/v1/attendance/today", "attendance.read"),
    # Holidays. {calendar} and {holiday} do not exist.
    Route("GET", "/api/v1/holiday-calendars", "holiday.read"),
    Route("GET", "/api/v1/holiday-calendars/{calendar}", "holiday.read", allowed_statuses=frozenset({404})),
    Route(
        "POST",
        "/api/v1/holiday-calendars",
        "holiday.manage",
        {"name": "Matrix calendar", "year": 2031},
        allowed_statuses=frozenset({201, 409}),
    ),
    Route(
        "PATCH",
        "/api/v1/holiday-calendars/{calendar}",
        "holiday.manage",
        {},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/holiday-calendars/{calendar}/holidays",
        "holiday.manage",
        {"date": "2031-01-01", "name": "Test holiday"},
        headers={"If-Match": '"1"'},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "PATCH",
        "/api/v1/holiday-calendars/{calendar}/holidays/{holiday}",
        "holiday.manage",
        {"name": "Test holiday"},
        headers={"If-Match": '"1"'},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "DELETE",
        "/api/v1/holiday-calendars/{calendar}/holidays/{holiday}",
        "holiday.manage",
        headers={"If-Match": '"1"'},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "PUT",
        "/api/v1/holiday-calendars/{calendar}/locations/{location}",
        "holiday.manage",
        headers={"If-Match": '"1"'},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "DELETE",
        "/api/v1/holiday-calendars/{calendar}/locations/{location}",
        "holiday.manage",
        headers={"If-Match": '"1"'},
        allowed_statuses=frozenset({404}),
    ),
    # HR accounts without an employee record have no location, so no own holidays (404).
    Route(
        "GET",
        "/api/v1/me/holidays",
        "holiday.read",
        params={"from": "2026-01-01", "to": "2026-12-31"},
        allowed_statuses=frozenset({200, 404}),
    ),
    # Linking an account: the service also needs employee.lifecycle.manage over the employee
    # (hidden as 404 without it) and a fresh step-up (403), whatever the permission flags say.
    Route(
        "POST",
        "/api/v1/users/{target}/employee-link",
        "user.invite",
        {"employee_id": str(uuid.UUID(int=4)), "reason": "Matrix test"},
        allowed_statuses=frozenset({403, 404}),
    ),
    # Leave. {leave_type} and {request} do not exist; matrix accounts have no leave policy.
    Route("GET", "/api/v1/leave-types", "leave.read"),
    Route(
        "POST",
        "/api/v1/leave-types",
        "leave.policy.manage",
        {
            "code": "MATRIX-LT",
            "name": "Matrix leave",
            "is_paid": True,
            "requires_approval": True,
            "allows_half_day": False,
            "counts_weekly_offs": False,
            "counts_holidays": False,
        },
        allowed_statuses=frozenset({201, 409}),
    ),
    Route(
        "PATCH",
        "/api/v1/leave-types/{leave_type}",
        "leave.policy.manage",
        {},
        headers={"If-Match": '"1"'},
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/leave-policies", "leave.read"),
    Route(
        "POST",
        "/api/v1/leave-policies",
        "leave.policy.manage",
        {
            "leave_type_id": str(uuid.UUID(int=5)),
            "effective_from": "2031-01-01",
            "leave_year_start_month": 1,
            "leave_year_start_day": 1,
            "accrual_method": "none",
            "allow_negative_balance": False,
        },
        allowed_statuses=frozenset({422}),
    ),
    # Accounts without an employee record have no balances of their own (404).
    Route("GET", "/api/v1/me/leave/balances", "leave.read", allowed_statuses=frozenset({200, 404})),
    Route("GET", "/api/v1/me/leave/ledger", "leave.read", allowed_statuses=frozenset({200, 404})),
    Route(
        "GET",
        "/api/v1/employees/{employee}/leave/balances",
        "leave.read",
        allowed_statuses=frozenset({200, 404}),
    ),
    Route(
        "GET",
        "/api/v1/employees/{employee}/leave/ledger",
        "leave.read",
        allowed_statuses=frozenset({200, 404}),
    ),
    Route(
        "POST",
        "/api/v1/employees/{employee}/leave-adjustments",
        "leave.balance.adjust",
        {
            "leave_type_id": str(uuid.UUID(int=5)),
            "entry_type": "adjustment",
            "amount": "1",
            "effective_date": "2031-01-01",
            "reason": "Matrix test",
        },
        headers={"Idempotency-Key": "matrix-adjustment"},
        allowed_statuses=frozenset({404, 422}),
    ),
    Route(
        "POST",
        "/api/v1/leave-allocations",
        "leave.balance.adjust",
        {"leave_type_id": str(uuid.UUID(int=5)), "as_of": "2031-01-01"},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/me/leave-requests/preview",
        "leave.request.self",
        {"leave_type_id": str(uuid.UUID(int=5)), "start_date": "2031-01-06", "end_date": "2031-01-06"},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/me/leave-requests",
        "leave.request.self",
        {"leave_type_id": str(uuid.UUID(int=5)), "start_date": "2031-01-06", "end_date": "2031-01-06"},
        headers={"Idempotency-Key": "matrix-leave-request"},
        allowed_statuses=frozenset({422}),
    ),
    Route("GET", "/api/v1/me/leave-requests", "leave.request.self"),
    Route(
        "POST",
        "/api/v1/me/leave-requests/{request}/cancel",
        "leave.request.self",
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/leave-requests", "leave.read"),
    Route("GET", "/api/v1/leave-requests/{request}", "leave.read", allowed_statuses=frozenset({404})),
    Route(
        "POST",
        "/api/v1/leave-requests/{request}/approve",
        "leave.request.approve",
        {},
        headers={"Idempotency-Key": "matrix-approve"},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/leave-requests/{request}/reject",
        "leave.request.approve",
        {"note": "Matrix test"},
        headers={"Idempotency-Key": "matrix-reject"},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "GET",
        "/api/v1/leave/calendar",
        "leave.read",
        params={"from": "2031-01-01", "to": "2031-01-31"},
    ),
    Route("GET", "/api/v1/leave/report", "leave.read", params={"from": "2031-01-01", "to": "2031-01-31"}),
    Route(
        "POST",
        "/api/v1/leave-year-end",
        "leave.balance.adjust",
        {"leave_type_id": str(uuid.UUID(int=5)), "year_end": "2020-12-31", "apply": False},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/leave-opening-balances",
        "leave.balance.adjust",
        {
            "reason": "Matrix test",
            "apply": False,
            "rows": [
                {
                    "employee_code": "NO-SUCH",
                    "leave_type_code": "NONE",
                    "days": "1",
                    "effective_date": "2031-01-01",
                }
            ],
        },
    ),
    # Attendance rules and corrections. {correction}, {shift} and {lock} do not exist; matrix
    # employees joined today, so a correction for an earlier date is refused (422).
    Route("GET", "/api/v1/attendance/register", "attendance.read", params={"date": "2026-01-15"}),
    Route(
        "GET",
        "/api/v1/attendance/report",
        "attendance.read",
        params={"from": "2026-01-01", "to": "2026-01-31"},
    ),
    Route("GET", "/api/v1/attendance/setup", "attendance.read"),
    Route(
        "POST",
        "/api/v1/me/attendance/corrections",
        "attendance.correction.request.self",
        {
            "work_date": "2026-01-05",
            "reason": "Matrix test",
            "items": [{"action": "add", "event_type": "CLOCK_IN", "proposed_at": "2026-01-05T04:00:00Z"}],
        },
        headers={"Idempotency-Key": "matrix-correction"},
        allowed_statuses=frozenset({422}),
    ),
    Route("GET", "/api/v1/me/attendance/corrections", "attendance.correction.request.self"),
    Route(
        "POST",
        "/api/v1/me/attendance/corrections/{correction}/cancel",
        "attendance.correction.request.self",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/employees/{employee}/attendance/corrections",
        "attendance.correction.create.all",
        {
            "work_date": "2026-01-05",
            "reason": "Matrix test",
            "items": [{"action": "add", "event_type": "CLOCK_IN", "proposed_at": "2026-01-05T04:00:00Z"}],
        },
        headers={"Idempotency-Key": "matrix-hr-correction"},
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/attendance/corrections", "attendance.read"),
    Route(
        "GET",
        "/api/v1/attendance/corrections/{correction}",
        "attendance.read",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/attendance/corrections/{correction}/approve",
        "attendance.correction.approve",
        {},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/attendance/corrections/{correction}/reject",
        "attendance.correction.approve",
        {"note": "Matrix test"},
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/shifts", "shift.read"),
    Route(
        "POST",
        "/api/v1/shifts",
        "shift.manage",
        {
            "code": "MATRIX-SHIFT",
            "name": "Matrix shift",
            "version": {
                "effective_from": "2031-01-01",
                "start_time": "09:00",
                "end_time": "17:00",
                "weekly_offs": [{"weekday": 7}],
            },
        },
        allowed_statuses=frozenset({201, 409}),
    ),
    Route("PATCH", "/api/v1/shifts/{shift}", "shift.manage", {}, allowed_statuses=frozenset({422})),
    Route(
        "POST",
        "/api/v1/shifts/{shift}/versions",
        "shift.manage",
        {"effective_from": "2031-01-01", "start_time": "09:00", "end_time": "17:00", "weekly_offs": []},
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/location-default-shifts", "shift.read"),
    Route(
        "POST",
        "/api/v1/location-default-shifts",
        "shift.manage",
        {
            "location_id": str(uuid.UUID(int=3)),
            "shift_id": str(uuid.UUID(int=6)),
            "effective_from": "2031-01-01",
        },
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "GET",
        "/api/v1/employees/{employee}/shift-assignments",
        "attendance.read",
        allowed_statuses=frozenset({200, 404}),
    ),
    Route(
        "POST",
        "/api/v1/shift-assignments",
        "shift.assign.all",
        {
            "employee_ids": [str(uuid.UUID(int=4))],
            "shift_id": str(uuid.UUID(int=6)),
            "effective_from": "2031-01-01",
        },
        allowed_statuses=frozenset({422}),
    ),
    Route("GET", "/api/v1/attendance-policies", "shift.read"),
    Route(
        "POST",
        "/api/v1/attendance-policies",
        "attendance.policy.manage",
        {
            "scope_type": "location",
            "scope_id": str(uuid.UUID(int=3)),
            "effective_from": "2031-01-01",
            "grace_minutes": 1,
            "early_departure_grace_minutes": 1,
            "duration_basis": "effective",
            "full_day_min_minutes": 2,
            "half_day_min_minutes": 1,
            "breaks_count_as_work": False,
            "break_excess_action": "flag",
            "overtime_enabled": False,
            "overtime_requires_approval": False,
            "incomplete_after_minutes": 1,
            "allow_multiple_sessions": True,
        },
        # The location does not exist.
        allowed_statuses=frozenset({422}),
    ),
    Route("GET", "/api/v1/attendance/locks", "attendance.read"),
    Route(
        "POST",
        "/api/v1/attendance/locks",
        "attendance.policy.manage",
        {"period_start": "2026-10-20", "period_end": "2026-10-19", "reason": "Matrix test"},
        # The range is reversed.
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/attendance/locks/{lock}/release",
        "attendance.policy.manage",
        {"reason": "Matrix test"},
        allowed_statuses=frozenset({404}),
    ),
    # Notifications (the account baseline: every role holds them).
    Route("GET", "/api/v1/me/notifications", "notification.read.self"),
    Route("GET", "/api/v1/me/notifications/unread-count", "notification.read.self"),
    Route("POST", "/api/v1/me/notifications/read-all", "notification.read.self"),
    Route(
        "POST",
        "/api/v1/me/notifications/{notification}/read",
        "notification.read.self",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "DELETE",
        "/api/v1/me/notifications/{notification}",
        "notification.read.self",
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/me/notification-preferences", "notification.read.self"),
    Route(
        "PUT",
        "/api/v1/me/notification-preferences",
        "notification.preference.update.self",
        {"changes": [{"type": "export.ready", "channel": "in_app", "enabled": True}]},
    ),
    # Documents. {category}, {document} and {version} do not exist; uploads name an unknown
    # category (422). Test APIs have no file storage, but every check before storage answers.
    Route("GET", "/api/v1/document-categories", "document.read"),
    Route(
        "POST",
        "/api/v1/document-categories",
        "document.category.manage",
        {
            "code": "MATRIX-DOC",
            "name": "Matrix category",
            "sensitivity": "internal",
            "employee_visible": True,
            "employee_can_upload": False,
            "requires_expiry": False,
        },
        allowed_statuses=frozenset({201, 409}),
    ),
    Route(
        "PATCH",
        "/api/v1/document-categories/{category}",
        "document.category.manage",
        {},
        allowed_statuses=frozenset({422}),
    ),
    Route("GET", "/api/v1/me/documents", "document.read.self"),
    Route(
        "GET", "/api/v1/employees/{employee}/documents", "document.read", allowed_statuses=frozenset({404})
    ),
    Route("GET", "/api/v1/company-documents", "document.company.read"),
    Route("GET", "/api/v1/documents/{document}", "document.read", allowed_statuses=frozenset({404})),
    Route(
        "POST",
        "/api/v1/documents/{document}/versions/{version}/download",
        "document.read",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "POST",
        "/api/v1/me/documents",
        "document.upload.self",
        params={"category_id": str(uuid.UUID(int=7)), "title": "Matrix document"},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/employees/{employee}/documents",
        "document.manage.all",
        params={"category_id": str(uuid.UUID(int=7)), "title": "Matrix document"},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST",
        "/api/v1/company-documents",
        "document.company.manage",
        params={"category_id": str(uuid.UUID(int=7)), "title": "Matrix document"},
        allowed_statuses=frozenset({422}),
    ),
    Route(
        "POST", "/api/v1/documents/{document}/versions", "document.read", allowed_statuses=frozenset({404})
    ),
    Route("PATCH", "/api/v1/documents/{document}", "document.read", {}, allowed_statuses=frozenset({422})),
    # Exports: an allowed request is queued (202); one account has one export in preparation at a
    # time, so its next export request in the same run is refused with 409 export.in_progress.
    Route(
        "POST",
        "/api/v1/exports/attendance-summary",
        "attendance.export",
        {"from": "2026-01-01", "to": "2026-01-31"},
        allowed_statuses=frozenset({202, 409}),
    ),
    Route(
        "POST",
        "/api/v1/exports/attendance-days",
        "attendance.export",
        {"from": "2026-01-01", "to": "2026-01-31"},
        allowed_statuses=frozenset({202, 409}),
    ),
    Route(
        "POST",
        "/api/v1/exports/leave-requests",
        "leave.export",
        {"from": "2026-01-01", "to": "2026-01-31"},
        allowed_statuses=frozenset({202, 409}),
    ),
    Route(
        "POST", "/api/v1/exports/leave-balances", "leave.export", {}, allowed_statuses=frozenset({202, 409})
    ),
    Route("POST", "/api/v1/exports/employees", "employee.export", {}, allowed_statuses=frozenset({202, 409})),
    Route(
        "POST",
        "/api/v1/exports/audit-log",
        "audit.export",
        {"from": "2026-01-01", "to": "2026-01-31"},
        allowed_statuses=frozenset({202, 409}),
    ),
    # People: exit and emergency contacts. {employee} does not exist.
    Route(
        "POST",
        "/api/v1/employees/{employee}/exit",
        "employee.lifecycle.manage",
        {"last_working_day": "2026-01-31", "reason": "Matrix test"},
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "GET",
        "/api/v1/employees/{employee}/emergency-contacts",
        "employee.personal.read",
        allowed_statuses=frozenset({404}),
    ),
    Route(
        "PUT",
        "/api/v1/employees/{employee}/emergency-contacts",
        "employee.personal.update.all",
        {"contacts": []},
        allowed_statuses=frozenset({404}),
    ),
    Route("GET", "/api/v1/me/emergency-contacts", "employee.personal.read.self"),
    Route("PUT", "/api/v1/me/emergency-contacts", "employee.personal.update.self", {"contacts": []}),
    # Last: activating break-glass rotates the caller's tokens (its role changed).
    Route(
        "POST",
        "/api/v1/role-grant-requests/break-glass",
        "role.elevation.break_glass",
        {"reason": "Matrix test"},
        allowed_statuses=frozenset({201}),
    ),
)

ASSIGNABLE_ROLES = [role for role in RoleKey if not ROLE_DEFINITIONS[role].derived]


def holds(role: RoleKey | None, permission: str) -> bool:
    """Whether the role holds the permission at some scope."""
    return bool(held_keys(role, permission))


def held_keys(role: RoleKey | None, permission: str) -> list[str]:
    """The catalog keys behind a route's permission that the role holds."""
    held = set(ACCOUNT_BASELINE)
    if role is not None:
        held |= ROLE_DEFINITIONS[role].permissions
    if role is RoleKey.MANAGER:
        # A manager is an employee with reports: both derived roles apply.
        held |= ROLE_DEFINITIONS[RoleKey.EMPLOYEE].permissions
    return [key for key in Authorizer.candidate_keys(permission) if key in held]


async def call(api: Api, client: httpx.AsyncClient, route: Route) -> httpx.Response:
    target = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    [stored] = await api.records(UserRole, {"user_id": target.user_id})
    assignment, role = stored.id, role_id(RoleKey.HR.value)
    path = route.path.format(
        target=target.user_id,
        assignment=assignment,
        role=role,
        session=uuid.uuid4(),
        device=uuid.uuid4(),
        grant_request=uuid.uuid4(),
        location=uuid.uuid4(),
        department=uuid.uuid4(),
        designation=uuid.uuid4(),
        employee=uuid.uuid4(),
        calendar=uuid.uuid4(),
        holiday=uuid.uuid4(),
        leave_type=uuid.uuid4(),
        request=uuid.uuid4(),
        correction=uuid.uuid4(),
        shift=uuid.uuid4(),
        lock=uuid.uuid4(),
        notification=uuid.uuid4(),
        category=uuid.uuid4(),
        document=uuid.uuid4(),
        version=uuid.uuid4(),
        erasure=uuid.uuid4(),
    )
    if route.path.endswith("documents") and route.method == "POST":
        # Uploads send the file itself as the body.
        return await client.request(
            route.method, path, content=b"%PDF-1.4 matrix", params=route.params, headers=route.headers
        )
    return await client.request(
        route.method, path, json=route.body, params=route.params, headers=route.headers
    )


async def matrix_account(api: Api, role: RoleKey | None) -> Account:
    """An account holding one role. `employee` and `manager` come from real employment and
    reporting lines, never from an assignment."""
    if role not in (RoleKey.EMPLOYEE, RoleKey.MANAGER):
        return await api.create_account(roles=(role.value,) if role else ())
    today = api.clock.now().date()
    async with api.database.unit_of_work() as session:
        units = await create_org_units(session)
        employee = await create_employee(session, joined=today)
        await add_job(session, units, employee, manager_id=None, effective_from=today)
        if role is RoleKey.MANAGER:
            report = await create_employee(session, joined=today)
            await add_job(session, units, report, manager_id=employee, effective_from=today)
    return await api.create_account(employee_id=employee)


@pytest.mark.parametrize("role", [None, RoleKey.EMPLOYEE, RoleKey.MANAGER, *ASSIGNABLE_ROLES], ids=str)
async def test_each_role_reaches_exactly_its_routes(api: Api, role: RoleKey | None) -> None:
    account = await matrix_account(api, role)
    async with api.client() as client:
        await api.sign_in(client, account)
        me = (await client.get("/api/v1/me")).json()
        if role in (RoleKey.EMPLOYEE, RoleKey.MANAGER):
            assert role.value in me["roles"]
        for route in ROUTES:
            response = await call(api, client, route)
            allowed = holds(role, route.permission)
            context = f"{role} {route.method} {route.path}: {response.status_code} {response.text}"
            if not allowed:
                assert response.status_code == 403, context
                assert response.json()["type"] == "/problems/forbidden", context
            elif any(PERMISSIONS[key].step_up for key in held_keys(role, route.permission)):
                assert response.status_code == 403, context
                assert response.json()["type"] == "/problems/step-up-required", context
            else:
                assert response.status_code in route.allowed_statuses, context

        # With a fresh step-up, the allowed SU routes succeed.
        su_routes = [
            r for r in ROUTES if any(PERMISSIONS[key].step_up for key in held_keys(role, r.permission))
        ]
        if su_routes:
            await api.step_up(client, account)
            for route in su_routes:
                response = await call(api, client, route)
                assert response.status_code in route.allowed_statuses, f"{role} {route.path}: {response.text}"


async def test_every_route_needs_a_session(api: Api) -> None:
    async with api.client() as client:
        for route in ROUTES:
            response = await call(api, client, route)
            assert response.status_code == 401, route
            assert response.json()["type"] == "/problems/unauthenticated"


async def test_a_revoked_session_reaches_nothing(api: Api) -> None:
    account = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, account)
        assert (await client.get("/api/v1/users")).status_code == 200
        await api.update(
            "sessions",
            {"user_id": account.user_id},
            {"revoked_at": api.clock.now(), "revoked_reason": "revoked_by_admin"},
        )
        assert (await client.get("/api/v1/users")).status_code == 401


async def test_a_removed_role_stops_applying_on_the_next_request(api: Api) -> None:
    account = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, account)
        assert (await client.get("/api/v1/users")).status_code == 200
        await api.update(
            "user_roles", {"user_id": account.user_id, "revoked_at": None}, {"revoked_at": api.clock.now()}
        )
        assert (await client.get("/api/v1/users")).status_code == 403


async def test_an_expired_or_future_assignment_does_not_apply(api: Api) -> None:
    now = api.clock.now()
    windows = [(now - timedelta(hours=2), now - timedelta(hours=1)), (now + timedelta(days=1), None)]
    for valid_from, valid_until in windows:
        account = await api.create_account()
        async with api.database.unit_of_work() as session:
            session.add(
                UserRole(
                    user_id=account.user_id,
                    role_id=role_id(RoleKey.HR.value),
                    role_key=RoleKey.HR.value,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    grant_reason="Cover",
                )
            )
        async with api.client() as client:
            await api.sign_in(client, account)
            assert (await client.get("/api/v1/users")).status_code == 403


async def test_a_denied_attempt_at_an_audited_permission_leaves_a_trace(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        assert (await client.get("/api/v1/audit-log")).status_code == 403
        # A denial on a non-audited permission is just a 403.
        assert (await client.get("/api/v1/roles")).status_code == 403
    events = await api.find(
        "security_events", {"user_id": account.user_id, "event_type": "access.denied_sensitive"}
    )
    assert [event["details"]["permission"] for event in events] == ["audit.read"]


async def test_audited_reads_are_recorded(api: Api) -> None:
    auditor = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    async with api.client() as client:
        await api.sign_in(client, auditor)
        assert (await client.get("/api/v1/security-events", params={"limit": 5})).status_code == 200
        assert (await client.get("/api/v1/audit-log", params={"limit": 5})).status_code == 200
    rows = [
        (row.action, row.permission_used)
        for row in await api.records(
            AuditLogRecord, {"actor_user_id": auditor.user_id}, sort=[("recorded_at", 1), ("id", 1)]
        )
    ]
    assert rows == [("security.events.read", "security.event.read"), ("audit.log.read", "audit.read")]
