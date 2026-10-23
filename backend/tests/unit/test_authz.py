"""Permission catalog, role matrix boundaries and the engine's decisions (docs/authorization-model.md).

The engine runs here against an in-memory `Relationships`, so each rule is checked in
isolation; the integration matrix checks the same rules against MongoDB and HTTP.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.platform.authz.catalog import CATALOG, PERMISSIONS, Permission, Scope
from app.platform.authz.context import UNRESTRICTED, Actor, Constraint, SessionScope
from app.platform.authz.engine import STEP_UP_WINDOW, AccessDenied, Authorizer, Placement, StepUpRequired
from app.platform.authz.roles import ACCOUNT_BASELINE, DATA_ROLES, ROLE_DEFINITIONS, RoleKey
from app.platform.db import Session, TenantScopeError

NOW = datetime(2030, 6, 1, 9, 0, tzinfo=UTC)
ORGANIZATION = uuid.uuid4()
# The fake relationships never touch the database; decisions only check the organization.
NO_SESSION = cast(Session, SimpleNamespace(tenant=ORGANIZATION))
TODAY = NOW.date()

# --- catalog and matrix ------------------------------------------------------------------


def test_permission_keys_follow_the_naming_rule() -> None:
    for permission in CATALOG:
        parts = permission.key.split(".")
        assert 2 <= len(parts) <= 4, permission.key
        if permission.scope is not None:
            assert permission.base + "." + permission.scope == permission.key


@pytest.mark.parametrize(
    "key",
    [
        "auth.session.revoke.all",
        "user.disable",
        "security.mfa.reset",
        "security.settings.manage",
        "role.assign",
        "role.elevation.request",
        "role.elevation.approve",
        "role.elevation.break_glass",
        "audit.export",
        "settings.manage",
        "employee.sensitive.read.all",
        "employee.sensitive.update.all",
        "employee.export",
        "attendance.export",
        "leave.export",
        "document.sensitive.read.all",
    ],
)
def test_documented_step_up_permissions_require_it(key: str) -> None:
    assert PERMISSIONS[key].step_up


def test_every_export_requires_step_up_and_is_audited() -> None:
    for permission in CATALOG:
        if permission.key.endswith(".export"):
            assert permission.step_up, permission.key
            assert permission.audit_reads, permission.key


ERASURE = frozenset({"employee.erasure.read", "employee.erasure.request", "employee.erasure.approve"})
DATA_PREFIXES = ("employee.", "attendance.", "leave.", "document.", "compensation.", "payroll.", "payslip.")


@pytest.mark.parametrize("role", [RoleKey.SYSTEM_ADMIN, RoleKey.SUPER_ADMIN])
def test_admin_roles_hold_no_employee_data(role: RoleKey) -> None:
    """Least privilege, fixed architecture (§4.1, §4.2): no data access for admins. The one
    exception is destructive, not a read: super admins request and approve the permanent
    erasure of a person's data (owner decision 2026-10-23), which shows nothing but the
    person's name and code."""
    held = ROLE_DEFINITIONS[role].permissions - ERASURE
    assert not [key for key in held if key.startswith(DATA_PREFIXES)]


def test_only_super_admins_erase_and_erasure_reads_nothing() -> None:
    holders = {role.key for role in ROLE_DEFINITIONS.values() if role.permissions & ERASURE}
    assert holders == {RoleKey.SUPER_ADMIN}
    assert ROLE_DEFINITIONS[RoleKey.SUPER_ADMIN].permissions >= ERASURE
    # Requesting and approving need step-up and are audited; reading shows a name and code only.
    assert all(
        PERMISSIONS[key].step_up and PERMISSIONS[key].audit_reads
        for key in ERASURE - {"employee.erasure.read"}
    )


def test_super_admin_manages_access_without_using_it() -> None:
    held = ROLE_DEFINITIONS[RoleKey.SUPER_ADMIN].permissions
    assert {"role.assign", "role.elevation.approve", "audit.read"} <= held
    assert not {"user.invite", "user.disable", "security.mfa.reset", "settings.manage"} & held


def test_manager_reaches_no_personal_or_sensitive_data() -> None:
    held = ROLE_DEFINITIONS[RoleKey.MANAGER].permissions
    assert not [key for key in held if ".personal." in key or ".sensitive." in key]
    assert all(key.endswith(".team") for key in held)


def test_hr_holds_no_role_or_security_management() -> None:
    for role in (RoleKey.HR, RoleKey.HR_ADMIN):
        held = ROLE_DEFINITIONS[role].permissions
        assert not [key for key in held if key.startswith(("role.assign", "role.elevation", "security."))]


def test_derived_roles_and_data_roles() -> None:
    assert {role.key for role in ROLE_DEFINITIONS.values() if role.derived} == {
        RoleKey.EMPLOYEE,
        RoleKey.MANAGER,
    }
    assert RoleKey.SUPER_ADMIN not in DATA_ROLES
    assert PERMISSIONS.keys() >= ACCOUNT_BASELINE


# --- engine ------------------------------------------------------------------------------

MANAGER, REPORT, OUTSIDER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
DEPT_A, DEPT_B, LOC_A = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class FakeRelationships:
    def __init__(self) -> None:
        self.team: dict[tuple[uuid.UUID, date], set[uuid.UUID]] = {(MANAGER, TODAY): {REPORT}}
        self.placements = {REPORT: Placement(DEPT_A, LOC_A), OUTSIDER: Placement(DEPT_B, LOC_A)}

    async def is_team_member(
        self, session: Any, manager_employee_id: uuid.UUID, employee_id: uuid.UUID, on: date
    ) -> bool:
        return employee_id in self.team.get((manager_employee_id, on), set())

    async def team_member_ids(
        self, session: Any, manager_employee_id: uuid.UUID, on: date
    ) -> frozenset[uuid.UUID]:
        return frozenset(self.team.get((manager_employee_id, on), set()))

    async def placement(self, session: Any, employee_id: uuid.UUID, on: date) -> Placement | None:
        return self.placements.get(employee_id)

    async def placed_employee_ids(
        self, session: Any, department_id: uuid.UUID | None, location_id: uuid.UUID | None, on: date
    ) -> frozenset[uuid.UUID]:
        return frozenset(
            employee
            for employee, placement in self.placements.items()
            if department_id in (None, placement.department_id)
            and location_id in (None, placement.location_id)
        )


class FixedClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


def actor(
    *keys: str,
    employee_id: uuid.UUID | None = MANAGER,
    step_up_at: datetime | None = None,
    **constraints: Constraint,
) -> Actor:
    grants = {key: (constraints.get(key.replace(".", "_"), UNRESTRICTED),) for key in keys}
    return Actor(
        organization_id=ORGANIZATION,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        session_scope=SessionScope.FULL,
        employee_id=employee_id,
        grants=grants,
        step_up_at=step_up_at,
    )


@pytest.fixture
def engine() -> Authorizer:
    return Authorizer(FakeRelationships(), FixedClock())


async def test_deny_by_default(engine: Authorizer) -> None:
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, actor(), "employee.profile.read", subject_employee_id=REPORT)
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, actor(), "user.read.all")


async def test_unknown_permissions_are_programming_errors(engine: Authorizer) -> None:
    with pytest.raises(ValueError, match="unknown permission"):
        await engine.require(NO_SESSION, actor(), "employee.salary.read")


async def test_self_scope_matches_only_the_actors_own_record(engine: Authorizer) -> None:
    me = actor("employee.profile.read.self", employee_id=REPORT)
    granted = await engine.require(NO_SESSION, me, "employee.profile.read", subject_employee_id=REPORT)
    assert granted.key == "employee.profile.read.self"
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, me, "employee.profile.read", subject_employee_id=OUTSIDER)


async def test_self_scope_needs_a_linked_employee_record(engine: Authorizer) -> None:
    unlinked = actor("employee.profile.read.self", employee_id=None)
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, unlinked, "employee.profile.read", subject_employee_id=REPORT)


async def test_team_scope_follows_the_reporting_line_on_the_date(engine: Authorizer) -> None:
    manager = actor("employee.profile.read.team")
    granted = await engine.require(NO_SESSION, manager, "employee.profile.read", subject_employee_id=REPORT)
    assert granted.key == "employee.profile.read.team"
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, manager, "employee.profile.read", subject_employee_id=OUTSIDER)
    # The same report on another date: no reporting line then.
    with pytest.raises(AccessDenied):
        await engine.require(
            NO_SESSION,
            manager,
            "employee.profile.read",
            subject_employee_id=REPORT,
            on=TODAY - timedelta(days=1),
        )


async def test_the_narrowest_sufficient_scope_is_reported(engine: Authorizer) -> None:
    both = actor("employee.profile.read.team", "employee.profile.read.all")
    granted = await engine.require(NO_SESSION, both, "employee.profile.read", subject_employee_id=REPORT)
    assert granted.key == "employee.profile.read.team"
    granted = await engine.require(NO_SESSION, both, "employee.profile.read", subject_employee_id=OUTSIDER)
    assert granted.key == "employee.profile.read.all"


async def test_all_scope_is_narrowed_by_department_and_location(engine: Authorizer) -> None:
    hr_for_a = actor("employee.profile.read.all", employee_profile_read_all=Constraint(department_id=DEPT_A))
    await engine.require(NO_SESSION, hr_for_a, "employee.profile.read", subject_employee_id=REPORT)
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, hr_for_a, "employee.profile.read", subject_employee_id=OUTSIDER)
    hr_for_loc = actor("employee.profile.read.all", employee_profile_read_all=Constraint(location_id=LOC_A))
    for subject in (REPORT, OUTSIDER):
        await engine.require(NO_SESSION, hr_for_loc, "employee.profile.read", subject_employee_id=subject)
    both = actor(
        "employee.profile.read.all",
        employee_profile_read_all=Constraint(department_id=DEPT_B, location_id=LOC_A),
    )
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, both, "employee.profile.read", subject_employee_id=REPORT)


async def test_an_unplaced_employee_is_outside_a_restricted_all_scope(engine: Authorizer) -> None:
    hr_for_a = actor("employee.profile.read.all", employee_profile_read_all=Constraint(department_id=DEPT_A))
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, hr_for_a, "employee.profile.read", subject_employee_id=uuid.uuid4())


async def test_hidden_existence_answers_404(engine: Authorizer) -> None:
    with pytest.raises(AccessDenied) as denied:
        await engine.require(
            NO_SESSION, actor(), "employee.personal.read", subject_employee_id=OUTSIDER, hide_existence=True
        )
    assert denied.value.problem_type.value == "not-found"


async def test_step_up_is_required_within_the_window(engine: Authorizer) -> None:
    with pytest.raises(StepUpRequired) as missing:
        await engine.require(NO_SESSION, actor("user.disable"), "user.disable")
    assert missing.value.extensions == {"max_age_seconds": 600}
    fresh = actor("user.disable", step_up_at=NOW - STEP_UP_WINDOW + timedelta(seconds=1))
    await engine.require(NO_SESSION, fresh, "user.disable")
    expired = actor("user.disable", step_up_at=NOW - STEP_UP_WINDOW - timedelta(seconds=1))
    with pytest.raises(StepUpRequired):
        await engine.require(NO_SESSION, expired, "user.disable")


async def test_step_up_is_checked_only_after_the_permission(engine: Authorizer) -> None:
    """A user without the permission learns nothing about whether it needs step-up."""
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, actor(), "user.disable")


async def test_scope_filter_unions_held_scopes_and_refuses_with_none(engine: Authorizer) -> None:
    with pytest.raises(AccessDenied):
        await engine.scope_filter(NO_SESSION, actor(), "attendance.read", "employee_id")
    unrestricted = await engine.scope_filter(
        NO_SESSION, actor("attendance.read.all"), "attendance.read", "employee_id"
    )
    assert unrestricted == {}
    combined = await engine.scope_filter(
        NO_SESSION, actor("attendance.read.self", "attendance.read.team"), "attendance.read", "employee_id"
    )
    assert set(combined["employee_id"]["$in"]) == {MANAGER, REPORT}
    restricted = await engine.scope_filter(
        NO_SESSION,
        actor("attendance.read.all", attendance_read_all=Constraint(department_id=DEPT_B)),
        "attendance.read",
        "employee_id",
    )
    assert restricted == {"employee_id": {"$in": [OUTSIDER]}}
    unlinked = await engine.scope_filter(
        NO_SESSION, actor("attendance.read.self", employee_id=None), "attendance.read", "employee_id"
    )
    assert unlinked == {"employee_id": {"$in": []}}


async def test_decisions_are_made_only_in_the_actors_organization(engine: Authorizer) -> None:
    other = cast(Session, SimpleNamespace(tenant=uuid.uuid4()))
    hr = actor("employee.lifecycle.manage")
    with pytest.raises(TenantScopeError):
        await engine.require(other, hr, "employee.lifecycle.manage", subject_employee_id=REPORT)
    with pytest.raises(TenantScopeError):
        await engine.scope_filter(other, actor("attendance.read.all"), "attendance.read", "employee_id")


def test_scopes_are_ordered(engine: Authorizer) -> None:
    assert Authorizer.candidate_keys("leave.read") == ("leave.read.all", "leave.read.team", "leave.read.self")
    assert Authorizer.candidate_keys("user.invite") == ("user.invite",)
    assert Permission("org.read", "x").scope is None
    assert Permission("leave.read.team", "x").scope is Scope.TEAM


async def test_an_unscoped_person_permission_reaches_everyone_within_its_constraint(
    engine: Authorizer,
) -> None:
    """`employee.lifecycle.manage` has no scope suffix but is about a person: like `all`."""
    hr = actor("employee.lifecycle.manage")
    for subject in (REPORT, OUTSIDER):
        granted = await engine.require(
            NO_SESSION, hr, "employee.lifecycle.manage", subject_employee_id=subject
        )
        assert granted.key == "employee.lifecycle.manage"
    hr_for_a = actor("employee.lifecycle.manage", employee_lifecycle_manage=Constraint(department_id=DEPT_A))
    await engine.require(NO_SESSION, hr_for_a, "employee.lifecycle.manage", subject_employee_id=REPORT)
    with pytest.raises(AccessDenied):
        await engine.require(NO_SESSION, hr_for_a, "employee.lifecycle.manage", subject_employee_id=OUTSIDER)
