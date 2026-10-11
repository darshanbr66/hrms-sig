"""Route coverage (docs/security-architecture.md §12, docs/authorization-model.md §9).

Deny by default is enforced by construction: every route declares exactly one access rule,
public and account-only routes are confined to their allow-lists, and every permission a
route names exists in the catalog. A catalog permission no route uses yet is listed below
with the checkpoint that brings it, so an unused permission cannot go unnoticed.
"""

from dataclasses import dataclass
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from app.main import _build
from app.platform.authz.catalog import PERMISSIONS
from app.platform.authz.engine import Authorizer
from app.platform.authz.route import ACCESS_ATTRIBUTE, ACCOUNT_ROUTES, PUBLIC_ROUTES
from tests.integration.test_authorization_matrix import ROUTES as MATRIX_ROUTES

# Permissions whose routes or service methods arrive later (m1-implementation-plan.md §10).
POLICIES = "M3 (shifts, attendance policies and corrections: waiting for Sigvitas's attendance policy values)"
IDENTIFIERS = "M2 (identifiers and bank accounts: Sigvitas has not yet chosen the identifier types)"
NOT_YET_USED = {
    "audit.export": "M5 (exports)",
    "employee.emergency_contact.read.team": "M2 (emergency contacts)",
    "employee.sensitive.read.self": IDENTIFIERS,
    "employee.sensitive.read.all": IDENTIFIERS,
    "employee.sensitive.update.all": IDENTIFIERS,
    "employee.export": "M5 (exports)",
    "leave.export": "M5 (exports)",
    "attendance.correction.request.self": POLICIES,
    "attendance.correction.approve.team": POLICIES,
    "attendance.correction.approve.all": POLICIES,
    "attendance.correction.create.all": POLICIES,
    "attendance.policy.manage": POLICIES,
    "attendance.export": "M5 (exports)",
}
# Permissions a service checks beyond the route's own (the route-level rule is broader).
SERVICE_CHECKED = {
    "security.settings.manage": "PUT /settings/{key} for security.* keys (modules/settings/service.py)",
}
# Domains whose modules do not exist yet (M2 onwards): their permissions are all unused.
LATER_DOMAINS = (
    "shift.",
    "document.",
    "notification.",
)


@dataclass(frozen=True)
class Route:
    path: str
    methods: frozenset[str]
    dependant: Dependant


def access_rules(route: Route) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    pending: list[Any] = [route.dependant]
    while pending:
        dependant = pending.pop()
        access = getattr(dependant.call, ACCESS_ATTRIBUTE, None)
        if access is not None:
            found.append(access)
        pending.extend(dependant.dependencies)
    return found


def api_routes() -> list[Route]:
    """Every effective route with its full path (FastAPI nests included routers)."""
    routes = [
        Route(context.path or "", frozenset(context.methods or ()), context.route.dependant)
        for context in iter_route_contexts(_build(docs_enabled=False).routes)
        if isinstance(context.route, APIRoute)
    ]
    assert len(routes) > 30
    return routes


def test_every_route_declares_exactly_one_access_rule() -> None:
    for route in api_routes():
        rules = access_rules(route)
        assert len(rules) == 1, f"{route.methods} {route.path} declares {rules}"


def test_public_and_account_routes_are_on_their_allow_lists() -> None:
    for route in api_routes():
        [rule] = access_rules(route)
        if rule[0] == "public":
            assert route.path in PUBLIC_ROUTES, route.path
        elif rule[0] == "account":
            assert route.path in ACCOUNT_ROUTES, route.path
        else:
            assert rule[0] == "permission"
            assert route.path not in PUBLIC_ROUTES, route.path
    paths = {route.path for route in api_routes()}
    assert paths >= PUBLIC_ROUTES
    assert paths >= ACCOUNT_ROUTES


def test_self_service_routes_take_no_user_or_employee_id() -> None:
    """CLAUDE.md rule 3."""
    for route in api_routes():
        if route.path.startswith("/api/v1/me"):
            assert "user_id" not in route.path
            assert "employee_id" not in route.path


def routed_permissions() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for route in api_routes():
        [rule] = access_rules(route)
        if rule[0] == "permission":
            # A route naming a permission base (`employee.profile.read`) uses every scoped key.
            for key in Authorizer.candidate_keys(rule[1]):
                for method in route.methods:
                    used.setdefault(key, set()).add(f"{method} {route.path}")
    return used


def test_every_catalog_permission_is_used_or_scheduled() -> None:
    used = routed_permissions()
    assert set(used) <= PERMISSIONS.keys()
    for key in PERMISSIONS:
        if key.startswith(LATER_DOMAINS):
            assert key not in used, f"{key} is routed; remove its domain from LATER_DOMAINS"
            continue
        assert (key in used or key in SERVICE_CHECKED) != (key in NOT_YET_USED), key


def test_the_authorization_matrix_covers_every_permission_route() -> None:
    names = {
        "{target}": "{user_id}",
        "{assignment}": "{assignment_id}",
        "{role}": "{role_id}",
        "{session}": "{session_id}",
        "{device}": "{device_id}",
        "{grant_request}": "{request_id}",
        "{location}": "{location_id}",
        "{department}": "{department_id}",
        "{designation}": "{designation_id}",
        "{employee}": "{employee_id}",
        "{calendar}": "{calendar_id}",
        "{holiday}": "{holiday_id}",
        "{leave_type}": "{leave_type_id}",
        "{request}": "{request_id}",
        # The matrix changes one concrete setting.
        "email.delivery.max_attempts": "{key}",
    }
    matrix = set()
    for route in MATRIX_ROUTES:
        path = route.path
        for placeholder, parameter in names.items():
            path = path.replace(placeholder, parameter)
        matrix.add(f"{route.method} {path}")
    routed = {endpoint for endpoints in routed_permissions().values() for endpoint in endpoints}
    assert routed == matrix
