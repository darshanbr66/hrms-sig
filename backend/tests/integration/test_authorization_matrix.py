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
    [(assignment,)] = await api.fetch(
        "SELECT id FROM access.user_roles WHERE user_id = :id", id=target.user_id
    )
    [(role,)] = await api.fetch("SELECT id FROM access.roles WHERE key = 'hr'")
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
        await api.execute(
            "UPDATE identity.sessions SET revoked_at = now(), revoked_reason = 'revoked_by_admin' "
            "WHERE user_id = :id",
            id=account.user_id,
        )
        assert (await client.get("/api/v1/users")).status_code == 401


async def test_a_removed_role_stops_applying_on_the_next_request(api: Api) -> None:
    account = await api.create_account(roles=(RoleKey.HR.value,))
    async with api.client() as client:
        await api.sign_in(client, account)
        assert (await client.get("/api/v1/users")).status_code == 200
        await api.execute(
            "UPDATE access.user_roles SET revoked_at = now() WHERE user_id = :id AND revoked_at IS NULL",
            id=account.user_id,
        )
        assert (await client.get("/api/v1/users")).status_code == 403


async def test_an_expired_or_future_assignment_does_not_apply(api: Api) -> None:
    now = api.clock.now()
    windows = [(now - timedelta(hours=2), now - timedelta(hours=1)), (now + timedelta(days=1), None)]
    for valid_from, valid_until in windows:
        account = await api.create_account()
        await api.execute(
            "INSERT INTO access.user_roles (user_id, role_id, valid_from, valid_until, grant_reason) "
            "SELECT :id, id, :valid_from, :valid_until, 'Cover' FROM access.roles WHERE key = 'hr'",
            id=account.user_id,
            valid_from=valid_from,
            valid_until=valid_until,
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
    events = await api.fetch(
        "SELECT details->>'permission' FROM audit.security_events "
        "WHERE user_id = :id AND event_type = 'access.denied_sensitive'",
        id=account.user_id,
    )
    assert events == [("audit.read",)]


async def test_audited_reads_are_recorded(api: Api) -> None:
    auditor = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    async with api.client() as client:
        await api.sign_in(client, auditor)
        assert (await client.get("/api/v1/security-events", params={"limit": 5})).status_code == 200
        assert (await client.get("/api/v1/audit-log", params={"limit": 5})).status_code == 200
    rows = await api.fetch(
        "SELECT action, permission_used FROM audit.audit_log "
        "WHERE actor_user_id = :id ORDER BY recorded_at, id",
        id=auditor.user_id,
    )
    assert rows == [("security.events.read", "security.event.read"), ("audit.log.read", "audit.read")]
