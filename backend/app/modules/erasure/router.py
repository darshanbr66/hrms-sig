"""HTTP routes for the permanent erasure of one person's data (docs/api-architecture.md §8,
Administration). Every route needs an erasure permission, which only super admins hold; the
service also requires the acting account to hold `super_admin`."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.modules.erasure.service import ErasureRecord, ErasureService
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires

router = APIRouter(prefix="/api/v1", tags=["erasure"])

type Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
type Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErasureCreate(_Request):
    # Typed by the requester, to say which person they mean.
    employee_code: Code
    # Why the erasure is authorized and necessary. Kept in the audit log: no personal details.
    reason: Reason


class ErasureApprove(_Request):
    # Typed again by the approver, to confirm the person named in the request.
    employee_code: Code
    note: Reason | None = None


class ErasureReject(_Request):
    note: Reason


class ErasureSubjectView(_Response):
    id: uuid.UUID
    # The name and code shown so both super admins can confirm the person; placeholders once
    # the erasure ran. No other data about the person.
    name: str
    employee_code: str


class ErasureUserRef(_Response):
    id: uuid.UUID
    email: str | None


class ErasureCategoryView(_Response):
    key: str
    outcome: Literal["deleted", "anonymized", "retained", "pending"]
    count: int
    note: str


class ErasureView(_Response):
    id: uuid.UUID
    status: Literal["pending", "rejected", "cancelled", "files_pending", "completed"]
    employee: ErasureSubjectView | None
    reason: str
    requested_by: ErasureUserRef
    requested_at: datetime
    decided_by: ErasureUserRef | None
    decided_at: datetime | None
    decision_note: str | None
    # What was done to each kind of record, once approved; null before.
    result: list[ErasureCategoryView] | None
    files_total: int
    files_removed: int
    # Stored files still to be removed by the worker; the request is complete only at 0.
    files_remaining: int
    last_file_error: str | None
    completed_at: datetime | None


class ErasureList(_Response):
    items: list[ErasureView]


def service(request: Request) -> ErasureService:
    value: ErasureService = request.app.state.erasure_service
    return value


Service = Annotated[ErasureService, Depends(service)]


def _ref(user_id: uuid.UUID | None, emails: dict[uuid.UUID, str]) -> ErasureUserRef | None:
    return ErasureUserRef(id=user_id, email=emails.get(user_id)) if user_id is not None else None


def _view(record: ErasureRecord) -> ErasureView:
    request, subject = record.request, record.subject
    requested_by = _ref(request.requested_by, record.emails)
    if requested_by is None:
        raise RuntimeError("an erasure request names no requester")
    return ErasureView(
        id=request.id,
        status=request.status,
        employee=ErasureSubjectView(
            id=subject.employee_id, name=subject.name, employee_code=subject.employee_code
        )
        if subject is not None
        else None,
        reason=request.reason,
        requested_by=requested_by,
        requested_at=request.requested_at,
        decided_by=_ref(request.decided_by, record.emails),
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        result=[ErasureCategoryView(**category) for category in request.result]
        if request.result is not None
        else None,
        files_total=request.files_total,
        files_removed=request.files_removed,
        files_remaining=len(request.pending_storage_keys),
        last_file_error=request.last_file_error,
        completed_at=request.completed_at,
    )


@router.get("/employee-erasures", response_model=ErasureList)
async def list_erasures(
    erasures: Service,
    actor: Annotated[Actor, requires("employee.erasure.read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ErasureList:
    """Erasure requests, newest first."""
    return ErasureList(items=[_view(record) for record in await erasures.list_requests(actor, limit=limit)])


@router.get("/employee-erasures/{erasure_id}", response_model=ErasureView)
async def get_erasure(
    erasure_id: uuid.UUID, erasures: Service, actor: Annotated[Actor, requires("employee.erasure.read")]
) -> ErasureView:
    return _view(await erasures.get(actor, erasure_id))


@router.post("/employee-erasures", response_model=ErasureView, status_code=status.HTTP_201_CREATED)
async def request_erasure(
    body: ErasureCreate,
    request: Request,
    erasures: Service,
    actor: Annotated[Actor, requires("employee.erasure.request")],
) -> ErasureView:
    """Ask for the permanent erasure of an offboarded person's data. Step-up. A different super
    admin must approve it; nothing is erased until then."""
    return _view(
        await erasures.request(
            actor, RequestContext.from_request(request), employee_code=body.employee_code, reason=body.reason
        )
    )


@router.post("/employee-erasures/{erasure_id}/approve", response_model=ErasureView)
async def approve_erasure(
    erasure_id: uuid.UUID,
    body: ErasureApprove,
    request: Request,
    erasures: Service,
    actor: Annotated[Actor, requires("employee.erasure.approve")],
) -> ErasureView:
    """Approve and run another super admin's request: the records are erased in one transaction
    and the stored files are removed by the worker. Step-up; the employee code typed again."""
    return _view(
        await erasures.approve(
            actor,
            RequestContext.from_request(request),
            erasure_id,
            employee_code=body.employee_code,
            note=body.note,
        )
    )


@router.post("/employee-erasures/{erasure_id}/reject", response_model=ErasureView)
async def reject_erasure(
    erasure_id: uuid.UUID,
    body: ErasureReject,
    request: Request,
    erasures: Service,
    actor: Annotated[Actor, requires("employee.erasure.approve")],
) -> ErasureView:
    return _view(await erasures.reject(actor, RequestContext.from_request(request), erasure_id, body.note))


@router.post("/employee-erasures/{erasure_id}/cancel", response_model=ErasureView)
async def cancel_erasure(
    erasure_id: uuid.UUID,
    request: Request,
    erasures: Service,
    actor: Annotated[Actor, requires("employee.erasure.request")],
) -> ErasureView:
    """The requester withdraws a request still waiting."""
    return _view(await erasures.cancel(actor, RequestContext.from_request(request), erasure_id))
