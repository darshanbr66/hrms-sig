"""HTTP routes for roles and role assignments (docs/api-architecture.md §8, Administration)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.modules.access import schemas as s
from app.modules.access.service import AccessService, Assignment, AssignmentRequest, RoleWithPermissions
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires

router = APIRouter(prefix="/api/v1", tags=["access"])


def service(request: Request) -> AccessService:
    value: AccessService = request.app.state.access_service
    return value


Service = Annotated[AccessService, Depends(service)]


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
        created_at=record.created_at,
    )


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


@router.post("/users/{user_id}/roles", response_model=s.AssignmentView, status_code=status.HTTP_201_CREATED)
async def assign_role(
    user_id: uuid.UUID,
    body: s.AssignmentCreate,
    request: Request,
    access: Service,
    actor: Annotated[Actor, requires("role.assign")],
) -> s.AssignmentView:
    assignment = await access.assign(
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
    return _assignment_view(assignment)


@router.delete("/users/{user_id}/roles/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_role(
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
    request: Request,
    access: Service,
    actor: Annotated[Actor, requires("role.assign")],
) -> None:
    await access.remove(actor, RequestContext.from_request(request), user_id, assignment_id)
