"""HTTP routes for roles, role assignments and grant requests (docs/api-architecture.md §8)."""

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.modules.access import schemas as s
from app.modules.access.grant_requests import (
    DECISION_WINDOW,
    ElevationRequest,
    GrantRequestService,
    effective_status,
)
from app.modules.access.models import GrantStatus, RoleGrantRequest
from app.modules.access.service import AccessService, Assignment, AssignmentRequest, RoleWithPermissions
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import account, requires
from app.platform.clock import Clock

router = APIRouter(prefix="/api/v1", tags=["access"])


def service(request: Request) -> AccessService:
    value: AccessService = request.app.state.access_service
    return value


def grant_service(request: Request) -> GrantRequestService:
    value: GrantRequestService = request.app.state.grant_request_service
    return value


def clock(request: Request) -> Clock:
    value: Clock = request.app.state.clock
    return value


Service = Annotated[AccessService, Depends(service)]
Grants = Annotated[GrantRequestService, Depends(grant_service)]


def _role_view(item: RoleWithPermissions) -> s.RoleView:
    return s.RoleView(
        id=item.role.id,
        key=item.role.key,
        name=item.role.name,
        description=item.role.description,
        is_derived=item.role.is_derived,
        permissions=list(item.permission_keys),
    )


def _assignment_view(item: Assignment) -> s.AssignmentView:
    record = item.record
    return s.AssignmentView(
        id=record.id,
        role_key=item.role_key,
        department_id=record.department_id,
        location_id=record.location_id,
        valid_from=record.valid_from,
        valid_until=record.valid_until,
        granted_by=record.granted_by,
        grant_reason=record.grant_reason,
        grant_request_id=record.grant_request_id,
        created_at=record.created_at,
    )


def _ref(user_id: uuid.UUID | None, emails: dict[uuid.UUID, str]) -> s.UserRef | None:
    return s.UserRef(id=user_id, email=emails.get(user_id)) if user_id is not None else None


def _grant_view(
    request: RoleGrantRequest, emails: dict[uuid.UUID, str], now_clock: Clock
) -> s.GrantRequestView:
    duration = request.requested_duration
    current = effective_status(request, now_clock.now())
    subject = _ref(request.subject_user_id, emails)
    requested_by = _ref(request.requested_by, emails)
    if subject is None or requested_by is None:
        raise RuntimeError("a grant request names no subject or requester")
    return s.GrantRequestView(
        id=request.id,
        kind=request.kind,
        status=current.value,
        subject=subject,
        role_key=request.role_key,
        department_id=request.department_id,
        location_id=request.location_id,
        reason=request.reason,
        duration_minutes=int(duration.total_seconds() // 60) if duration is not None else None,
        requested_by=requested_by,
        requested_at=request.requested_at,
        decide_by=request.requested_at + DECISION_WINDOW if current is GrantStatus.PENDING else None,
        decided_by=_ref(request.decided_by, emails),
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        starts_at=request.starts_at,
        ends_at=request.ends_at,
        revoked_by=_ref(request.revoked_by, emails),
        revoked_at=request.revoked_at,
        acknowledged_by=_ref(request.acknowledged_by, emails),
        acknowledged_at=request.acknowledged_at,
    )


async def _one(grants: GrantRequestService, request: RoleGrantRequest, http: Request) -> s.GrantRequestView:
    return _grant_view(request, await grants.emails([request]), clock(http))


@router.get("/roles", response_model=s.RoleList)
async def list_roles(access: Service, _actor: Annotated[Actor, requires("role.read")]) -> s.RoleList:
    return s.RoleList(items=[_role_view(item) for item in await access.list_roles()])


@router.get("/roles/{role_id}", response_model=s.RoleView)
async def get_role(
    role_id: uuid.UUID, access: Service, _actor: Annotated[Actor, requires("role.read")]
) -> s.RoleView:
    return _role_view(await access.get_role(role_id))


@router.get("/users/{user_id}/roles", response_model=s.AssignmentList)
async def user_roles(
    user_id: uuid.UUID, access: Service, _actor: Annotated[Actor, requires("role.read")]
) -> s.AssignmentList:
    return s.AssignmentList(items=[_assignment_view(item) for item in await access.assignments_of(user_id)])


@router.post(
    "/users/{user_id}/roles",
    response_model=s.AssignmentView | s.GrantRequestView,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": s.GrantRequestView,
            "description": "For `super_admin`: a request that a second super admin must approve.",
        }
    },
)
async def assign_role(
    *,
    user_id: uuid.UUID,
    body: s.AssignmentCreate,
    request: Request,
    response: Response,
    access: Service,
    grants: Grants,
    actor: Annotated[Actor, requires("role.assign")],
) -> s.AssignmentView | s.GrantRequestView:
    result = await access.assign(
        actor,
        RequestContext.from_request(request),
        user_id,
        AssignmentRequest(
            role_key=body.role_key,
            reason=body.reason,
            department_id=body.department_id,
            location_id=body.location_id,
            valid_until=body.valid_until,
        ),
    )
    if isinstance(result, RoleGrantRequest):
        response.status_code = status.HTTP_202_ACCEPTED
        return await _one(grants, result, request)
    return _assignment_view(result)


@router.post(
    "/users/{user_id}/roles/batch", response_model=s.AssignmentList, status_code=status.HTTP_201_CREATED
)
async def assign_roles(
    user_id: uuid.UUID,
    body: s.AssignmentBatchCreate,
    request: Request,
    access: Service,
    actor: Annotated[Actor, requires("role.assign")],
) -> s.AssignmentList:
    """Assign several roles in one transaction with one step-up; all or none. Roles already
    held are kept. `super_admin` is refused here (`409 role.super_admin_alone`): it is
    assigned on its own, as a request a second super admin approves."""
    assigned = await access.assign_many(
        actor,
        RequestContext.from_request(request),
        user_id,
        [
            AssignmentRequest(
                role_key=choice.role_key,
                reason=body.reason,
                department_id=choice.department_id,
                location_id=choice.location_id,
                valid_until=choice.valid_until,
            )
            for choice in body.roles
        ],
    )
    return s.AssignmentList(items=[_assignment_view(item) for item in assigned])


@router.delete("/users/{user_id}/roles/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_role(
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
    request: Request,
    access: Service,
    actor: Annotated[Actor, requires("role.assign")],
) -> None:
    await access.remove(actor, RequestContext.from_request(request), user_id, assignment_id)


# --- grant requests ---------------------------------------------------------------------------


@router.get("/role-grant-requests", response_model=s.GrantRequestPage)
async def list_grant_requests(
    *,
    request: Request,
    grants: Grants,
    actor: Annotated[Actor, requires("role.read")],
    status_filter: Annotated[s.GrantStatusValue | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
) -> s.GrantRequestPage:
    page = await grants.list(
        actor, status=GrantStatus(status_filter) if status_filter else None, before=cursor, limit=limit
    )
    now_clock = clock(request)
    return s.GrantRequestPage(
        items=[_grant_view(item, page.emails, now_clock) for item in page.items], next_cursor=page.next_cursor
    )


@router.post("/role-grant-requests", response_model=s.GrantRequestView, status_code=status.HTTP_201_CREATED)
async def request_elevation(
    body: s.ElevationCreate,
    request: Request,
    grants: Grants,
    actor: Annotated[Actor, requires("role.elevation.request")],
) -> s.GrantRequestView:
    """Ask for a role for yourself for a limited time. Another super admin must approve it."""
    created = await grants.request_elevation(
        actor,
        RequestContext.from_request(request),
        ElevationRequest(
            role_key=body.role_key,
            reason=body.reason,
            duration=timedelta(minutes=body.duration_minutes),
            department_id=body.department_id,
            location_id=body.location_id,
        ),
    )
    return await _one(grants, created, request)


@router.post(
    "/role-grant-requests/break-glass", response_model=s.GrantRequestView, status_code=status.HTTP_201_CREATED
)
async def break_glass(
    body: s.BreakGlassCreate,
    request: Request,
    grants: Grants,
    actor: Annotated[Actor, requires("role.elevation.break_glass")],
) -> s.GrantRequestView:
    """Emergency only: `system_admin` for yourself for at most an hour, without approval."""
    created = await grants.break_glass(
        actor, RequestContext.from_request(request), body.reason, timedelta(minutes=body.duration_minutes)
    )
    return await _one(grants, created, request)


@router.post("/role-grant-requests/{request_id}/approve", response_model=s.GrantRequestView)
async def approve_grant_request(
    request_id: uuid.UUID,
    body: s.GrantDecision,
    request: Request,
    grants: Grants,
    actor: Annotated[Actor, requires("role.elevation.approve")],
) -> s.GrantRequestView:
    decided = await grants.approve(actor, RequestContext.from_request(request), request_id, body.note)
    return await _one(grants, decided, request)


@router.post("/role-grant-requests/{request_id}/reject", response_model=s.GrantRequestView)
async def reject_grant_request(
    request_id: uuid.UUID,
    body: s.GrantDecision,
    request: Request,
    grants: Grants,
    actor: Annotated[Actor, requires("role.elevation.approve")],
) -> s.GrantRequestView:
    decided = await grants.reject(actor, RequestContext.from_request(request), request_id, body.note)
    return await _one(grants, decided, request)


@router.post("/role-grant-requests/{request_id}/revoke", response_model=s.GrantRequestView)
async def revoke_grant_request(
    request_id: uuid.UUID,
    request: Request,
    grants: Grants,
    actor: Annotated[Actor, requires("role.elevation.approve")],
) -> s.GrantRequestView:
    """Withdraw a pending request, or end an active elevation or break-glass now."""
    revoked = await grants.revoke(actor, RequestContext.from_request(request), request_id)
    return await _one(grants, revoked, request)


@router.post("/role-grant-requests/{request_id}/acknowledge", response_model=s.GrantRequestView)
async def acknowledge_break_glass(
    request_id: uuid.UUID,
    request: Request,
    grants: Grants,
    actor: Annotated[Actor, requires("role.elevation.approve")],
) -> s.GrantRequestView:
    """Record the post-incident review of a break-glass use (by another super admin)."""
    acknowledged = await grants.acknowledge(actor, RequestContext.from_request(request), request_id)
    return await _one(grants, acknowledged, request)


@router.get("/me/elevation", response_model=s.CurrentElevation)
async def my_elevation(
    request: Request, grants: Grants, actor: Annotated[Actor, account()]
) -> s.CurrentElevation:
    current = await grants.current_elevation(actor)
    return s.CurrentElevation(elevation=await _one(grants, current, request) if current else None)
