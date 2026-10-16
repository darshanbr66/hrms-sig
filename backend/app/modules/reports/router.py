"""HTTP routes for exports (docs/api-architecture.md §8, Reports).

One route per export type, so each declares the `*.export` permission it needs. The list of
one's own exports and their download are on the account allow-list: they answer only for
exports the signed-in person asked for.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.modules.reports.models import ExportJob, ExportType
from app.modules.reports.service import ExportRequest, ExportService
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import account, requires

router = APIRouter(prefix="/api/v1", tags=["reports"])

type ExportTypeValue = Literal[
    "attendance_summary", "attendance_days", "leave_requests", "leave_balances", "employees", "audit_log"
]


class PeriodBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start: date = Field(alias="from")
    end: date = Field(alias="to")
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None


class AuditBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start: date = Field(alias="from")
    end: date = Field(alias="to")


class ExportView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    export_type: ExportTypeValue
    status: Literal["queued", "running", "done", "failed", "expired"]
    start: date | None
    end: date | None
    requested_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    downloaded_at: datetime | None
    row_count: int | None
    file_name: str | None


class ExportList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExportView]
    # False when file storage is not set up: exports cannot be produced.
    storage_ready: bool


class ExportDownloadLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    expires_in_seconds: int


def service(request: Request) -> ExportService:
    value: ExportService = request.app.state.export_service
    return value


Service = Annotated[ExportService, Depends(service)]


def _view(job: ExportJob) -> ExportView:
    start, end = job.parameters.get("start"), job.parameters.get("end")
    return ExportView(
        id=job.id,
        export_type=job.export_type,
        status=job.status,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end) if end else None,
        requested_at=job.requested_at,
        completed_at=job.completed_at,
        expires_at=job.expires_at,
        downloaded_at=job.downloaded_at,
        row_count=job.row_count,
        file_name=job.file_name,
    )


async def _request(exports: ExportService, actor: Actor, request: Request, body: ExportRequest) -> ExportView:
    return _view(await exports.request(actor, RequestContext.from_request(request), body))


@router.post("/exports/attendance-summary", response_model=ExportView, status_code=status.HTTP_202_ACCEPTED)
async def export_attendance_summary(
    body: PeriodBody,
    request: Request,
    exports: Service,
    actor: Annotated[Actor, requires("attendance.export")],
) -> ExportView:
    """Per person: days per status and the period's totals. Needs a recent step-up."""
    return await _request(
        exports,
        actor,
        request,
        ExportRequest(
            ExportType.ATTENDANCE_SUMMARY, body.start, body.end, body.department_id, body.location_id
        ),
    )


@router.post("/exports/attendance-days", response_model=ExportView, status_code=status.HTTP_202_ACCEPTED)
async def export_attendance_days(
    body: PeriodBody,
    request: Request,
    exports: Service,
    actor: Annotated[Actor, requires("attendance.export")],
) -> ExportView:
    """One row per person and day, as judged. Needs a recent step-up."""
    return await _request(
        exports,
        actor,
        request,
        ExportRequest(ExportType.ATTENDANCE_DAYS, body.start, body.end, body.department_id, body.location_id),
    )


@router.post("/exports/leave-requests", response_model=ExportView, status_code=status.HTTP_202_ACCEPTED)
async def export_leave_requests(
    body: PeriodBody, request: Request, exports: Service, actor: Annotated[Actor, requires("leave.export")]
) -> ExportView:
    """Leave requests overlapping the period, without reasons. Needs a recent step-up."""
    return await _request(
        exports,
        actor,
        request,
        ExportRequest(ExportType.LEAVE_REQUESTS, body.start, body.end, body.department_id, body.location_id),
    )


@router.post("/exports/leave-balances", response_model=ExportView, status_code=status.HTTP_202_ACCEPTED)
async def export_leave_balances(
    body: ScopeBody, request: Request, exports: Service, actor: Annotated[Actor, requires("leave.export")]
) -> ExportView:
    """Each person's balance per leave type on the day the file is produced."""
    return await _request(
        exports,
        actor,
        request,
        ExportRequest(ExportType.LEAVE_BALANCES, None, None, body.department_id, body.location_id),
    )


@router.post("/exports/employees", response_model=ExportView, status_code=status.HTTP_202_ACCEPTED)
async def export_employees(
    body: ScopeBody, request: Request, exports: Service, actor: Annotated[Actor, requires("employee.export")]
) -> ExportView:
    """Employee records (job details and dates; no personal details)."""
    return await _request(
        exports,
        actor,
        request,
        ExportRequest(ExportType.EMPLOYEES, None, None, body.department_id, body.location_id),
    )


@router.post("/exports/audit-log", response_model=ExportView, status_code=status.HTTP_202_ACCEPTED)
async def export_audit_log(
    body: AuditBody, request: Request, exports: Service, actor: Annotated[Actor, requires("audit.export")]
) -> ExportView:
    """The audit log recorded in the period. Needs a recent step-up."""
    return await _request(exports, actor, request, ExportRequest(ExportType.AUDIT_LOG, body.start, body.end))


@router.get("/me/exports", response_model=ExportList)
async def my_exports(exports: Service, actor: Annotated[Actor, account()]) -> ExportList:
    """Exports you asked for, newest first."""
    return ExportList(
        items=[_view(job) for job in await exports.mine(actor)], storage_ready=exports.configured
    )


@router.post("/me/exports/{export_id}/download", response_model=ExportDownloadLink)
async def download_export(
    export_id: uuid.UUID, request: Request, exports: Service, actor: Annotated[Actor, account()]
) -> ExportDownloadLink:
    """A 60-second link to an export you asked for, until it expires. Audited."""
    url, seconds = await exports.download(actor, RequestContext.from_request(request), export_id)
    return ExportDownloadLink(url=url, expires_in_seconds=seconds)
