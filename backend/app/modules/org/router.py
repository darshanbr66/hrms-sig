"""HTTP routes for locations, departments and designations (docs/api-architecture.md §8)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from app.modules.org import schemas as s
from app.modules.org.models import Department, Designation, Location
from app.modules.org.service import OrgRecord, OrgService
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.concurrency import IfMatch, etag, expected_version

router = APIRouter(prefix="/api/v1", tags=["organization"])


def service(request: Request) -> OrgService:
    value: OrgService = request.app.state.org_service
    return value


Service = Annotated[OrgService, Depends(service)]


def _values(body: BaseModel) -> dict[str, Any]:
    """Only the fields the client sent, so PATCH can tell "leave as is" from "clear"."""
    return {name: getattr(body, name) for name in body.model_fields_set}


def _location(record: Location) -> s.LocationView:
    return s.LocationView.model_validate(record, from_attributes=True)


def _department(record: Department) -> s.DepartmentView:
    return s.DepartmentView.model_validate(record, from_attributes=True)


def _designation(record: Designation) -> s.DesignationView:
    return s.DesignationView.model_validate(record, from_attributes=True)


def _tagged(response: Response, record: OrgRecord) -> None:
    response.headers["ETag"] = etag(record.version)


# --- locations ----------------------------------------------------------------------------------


@router.get("/locations", response_model=s.LocationList)
async def list_locations(
    org: Service, _actor: Annotated[Actor, requires("org.read")], include_archived: bool = False
) -> s.LocationList:
    return s.LocationList(
        items=[_location(r) for r in await org.records(Location, include_archived=include_archived)]
    )


@router.post("/locations", response_model=s.LocationView, status_code=status.HTTP_201_CREATED)
async def create_location(
    body: s.LocationCreate,
    request: Request,
    response: Response,
    org: Service,
    actor: Annotated[Actor, requires("org.manage")],
) -> s.LocationView:
    record = await org.create(actor, RequestContext.from_request(request), Location, body.model_dump())
    _tagged(response, record)
    return _location(record)


@router.get("/locations/{location_id}", response_model=s.LocationView)
async def get_location(
    location_id: uuid.UUID, response: Response, org: Service, _actor: Annotated[Actor, requires("org.read")]
) -> s.LocationView:
    record = await org.get(Location, location_id)
    _tagged(response, record)
    return _location(record)


@router.patch("/locations/{location_id}", response_model=s.LocationView)
async def update_location(
    *,
    location_id: uuid.UUID,
    body: s.LocationUpdate,
    request: Request,
    response: Response,
    org: Service,
    actor: Annotated[Actor, requires("org.manage")],
    if_match: IfMatch = None,
) -> s.LocationView:
    record = await org.update(
        actor,
        RequestContext.from_request(request),
        Location,
        location_id,
        expected_version=expected_version(if_match),
        values=_values(body),
    )
    _tagged(response, record)
    return _location(record)


# --- departments --------------------------------------------------------------------------------


@router.get("/departments", response_model=s.DepartmentList)
async def list_departments(
    org: Service, _actor: Annotated[Actor, requires("org.read")], include_archived: bool = False
) -> s.DepartmentList:
    items = await org.records(Department, include_archived=include_archived)
    return s.DepartmentList(items=[_department(r) for r in items])


@router.post("/departments", response_model=s.DepartmentView, status_code=status.HTTP_201_CREATED)
async def create_department(
    body: s.DepartmentCreate,
    request: Request,
    response: Response,
    org: Service,
    actor: Annotated[Actor, requires("org.manage")],
) -> s.DepartmentView:
    record = await org.create(actor, RequestContext.from_request(request), Department, body.model_dump())
    _tagged(response, record)
    return _department(record)


@router.get("/departments/{department_id}", response_model=s.DepartmentView)
async def get_department(
    department_id: uuid.UUID, response: Response, org: Service, _actor: Annotated[Actor, requires("org.read")]
) -> s.DepartmentView:
    record = await org.get(Department, department_id)
    _tagged(response, record)
    return _department(record)


@router.patch("/departments/{department_id}", response_model=s.DepartmentView)
async def update_department(
    *,
    department_id: uuid.UUID,
    body: s.DepartmentUpdate,
    request: Request,
    response: Response,
    org: Service,
    actor: Annotated[Actor, requires("org.manage")],
    if_match: IfMatch = None,
) -> s.DepartmentView:
    record = await org.update(
        actor,
        RequestContext.from_request(request),
        Department,
        department_id,
        expected_version=expected_version(if_match),
        values=_values(body),
    )
    _tagged(response, record)
    return _department(record)


# --- designations -------------------------------------------------------------------------------


@router.get("/designations", response_model=s.DesignationList)
async def list_designations(
    org: Service, _actor: Annotated[Actor, requires("org.read")], include_archived: bool = False
) -> s.DesignationList:
    items = await org.records(Designation, include_archived=include_archived)
    return s.DesignationList(items=[_designation(r) for r in items])


@router.post("/designations", response_model=s.DesignationView, status_code=status.HTTP_201_CREATED)
async def create_designation(
    body: s.DesignationCreate,
    request: Request,
    response: Response,
    org: Service,
    actor: Annotated[Actor, requires("org.manage")],
) -> s.DesignationView:
    record = await org.create(actor, RequestContext.from_request(request), Designation, body.model_dump())
    _tagged(response, record)
    return _designation(record)


@router.get("/designations/{designation_id}", response_model=s.DesignationView)
async def get_designation(
    designation_id: uuid.UUID,
    response: Response,
    org: Service,
    _actor: Annotated[Actor, requires("org.read")],
) -> s.DesignationView:
    record = await org.get(Designation, designation_id)
    _tagged(response, record)
    return _designation(record)


@router.patch("/designations/{designation_id}", response_model=s.DesignationView)
async def update_designation(
    *,
    designation_id: uuid.UUID,
    body: s.DesignationUpdate,
    request: Request,
    response: Response,
    org: Service,
    actor: Annotated[Actor, requires("org.manage")],
    if_match: IfMatch = None,
) -> s.DesignationView:
    record = await org.update(
        actor,
        RequestContext.from_request(request),
        Designation,
        designation_id,
        expected_version=expected_version(if_match),
        values=_values(body),
    )
    _tagged(response, record)
    return _designation(record)
