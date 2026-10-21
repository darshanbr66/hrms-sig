"""Exports (product-requirements RPT-1 to RPT-3, authorization-model §3.7).

- Each export type needs its domain's `*.export` permission, which needs a recent step-up.
- A report never grants access (RPT-1): when the export is requested, the employees it covers
  are fixed to those the requester can read with the domain's read permission on that day
  (and in the department or location asked for). The worker then produces exactly that.
- Exports run in the worker (RPT-2). The file is stored privately, can be downloaded only by
  the person who asked for it, through a 60-second link, until 24 hours after it was produced
  (RPT-3), and its cells are protected against spreadsheet formulas. Requesting and
  downloading are both audited.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from app.modules.people import public as people
from app.modules.reports.models import ExportJob, ExportStatus, ExportType
from app.platform.audit.records import AuditEvent, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.files import DOWNLOAD_SECONDS, FileStore, FileStoreError
from app.platform.ratelimit import RateLimiter, RateLimitRule, RateLimitScope

# docs/api-architecture.md §9: both fail closed (503) when the limit cannot be checked.
EXPORTS_PER_USER: Final = RateLimitRule(RateLimitScope.EXPORT, "exports", 5, timedelta(hours=1))
EXPORT_DOWNLOADS_PER_USER: Final = RateLimitRule(
    RateLimitScope.DOWNLOAD, "export_downloads", 60, timedelta(hours=1)
)
MAX_LISTED: Final = 50


@dataclass(frozen=True, slots=True)
class Spec:
    permission: str
    # The read permission whose scope decides who the export covers; None for the audit log.
    scope: str | None
    # The longest period, in days; None for an export as of today.
    max_days: int | None
    title: str


SPECS: Final[dict[ExportType, Spec]] = {
    ExportType.ATTENDANCE_SUMMARY: Spec("attendance.export", "attendance.read", 62, "attendance-summary"),
    ExportType.ATTENDANCE_DAYS: Spec("attendance.export", "attendance.read", 31, "attendance-days"),
    ExportType.LEAVE_REQUESTS: Spec("leave.export", "leave.read", 366, "leave-requests"),
    ExportType.LEAVE_BALANCES: Spec("leave.export", "leave.read", None, "leave-balances"),
    ExportType.EMPLOYEES: Spec("employee.export", "employee.profile.read", None, "employees"),
    ExportType.AUDIT_LOG: Spec("audit.export", None, 93, "audit-log"),
}


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


@dataclass(frozen=True, slots=True)
class ExportRequest:
    export_type: ExportType
    start: date | None
    end: date | None
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None


class ExportService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        audit: AuditWriter,
        authorizer: Authorizer,
        rate_limiter: RateLimiter,
        store: FileStore,
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer
        self._limiter = rate_limiter
        self._store = store

    def _period(self, spec: Spec, request: ExportRequest) -> tuple[date | None, date | None]:
        today = self._authz.today()
        if spec.max_days is None:
            return None, None
        if request.start is None or request.end is None:
            raise field_error("from", "export.period_required", "Choose the first and last day.")
        if request.end < request.start:
            raise field_error("to", "range.reversed", "The end date must be on or after the start date.")
        if (request.end - request.start).days >= spec.max_days:
            raise field_error("to", "range.too_long", f"Choose a period of at most {spec.max_days} days.")
        if request.start > today:
            raise field_error("from", "export.future_period", "Choose a period that has already started.")
        return request.start, min(request.end, today)

    async def request(self, actor: Actor, context: RequestContext, request: ExportRequest) -> ExportJob:
        spec = SPECS[request.export_type]
        start, end = self._period(spec, request)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, spec.permission)
            await self._limiter.enforce(EXPORTS_PER_USER, str(actor.user_id))
            # One export in preparation per person at a time (api-architecture §9). The lock
            # makes one of two simultaneous requests of one person fail with a conflict.
            await session.lock(f"exports:{actor.user_id}")
            preparing = await session.exists(
                ExportJob,
                {
                    "requested_by": actor.user_id,
                    "status": {"$in": [ExportStatus.QUEUED.value, ExportStatus.RUNNING.value]},
                },
            )
            if preparing:
                raise ProblemError(
                    ProblemType.CONFLICT,
                    detail="Your previous export is still being prepared. Wait until it is ready, "
                    "then ask again.",
                    extensions={"code": "export.in_progress"},
                )
            parameters: dict[str, Any] = {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "department_id": str(request.department_id) if request.department_id else None,
                "location_id": str(request.location_id) if request.location_id else None,
            }
            covered = None
            if spec.scope is not None:
                on = end or self._authz.today()
                scope = await self._authz.scope_filter(
                    session, actor, spec.scope, people.EMPLOYEE_ID_FIELD, on=on
                )
                if request.department_id or request.location_id:
                    placed = await people.placed_employee_ids(
                        session, request.department_id, request.location_id, on
                    )
                    scope = (
                        {"$and": [scope, {people.EMPLOYEE_ID_FIELD: {"$in": sorted(placed)}}]}
                        if scope
                        else {people.EMPLOYEE_ID_FIELD: {"$in": sorted(placed)}}
                    )
                ids = await people.ids_in_scope(session, scope)
                parameters["employee_ids"] = [str(i) for i in ids]
                covered = len(ids)
            job = ExportJob(
                requested_by=actor.user_id, export_type=request.export_type.value, parameters=parameters
            )
            session.add(job)
            await session.flush()
            await self._audit.record(
                session,
                AuditEvent(
                    action="export.requested",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=granted.key,
                    target_type="export_job",
                    target_id=job.id,
                    changes={
                        "export_type": ValueChange(None, job.export_type),
                        "start": ValueChange(None, start),
                        "end": ValueChange(None, end),
                        **({"employees": ValueChange(None, covered)} if covered is not None else {}),
                    },
                ),
            )
            await session.refresh(job)
            return job

    async def mine(self, actor: Actor) -> list[ExportJob]:
        async with self._db.unit_of_work() as session:
            return await session.find(
                ExportJob,
                {"requested_by": actor.user_id},
                sort=[("requested_at", -1), ("id", -1)],
                limit=MAX_LISTED,
            )

    async def download(self, actor: Actor, context: RequestContext, job_id: uuid.UUID) -> tuple[str, int]:
        now = self._clock.now()
        await self._limiter.enforce(EXPORT_DOWNLOADS_PER_USER, str(actor.user_id))
        async with self._db.unit_of_work() as session:
            job = await session.get(ExportJob, job_id, for_update=True)
            # Only the person who asked for an export can download it (RPT-3).
            if job is None or job.requested_by != actor.user_id:
                raise ProblemError(ProblemType.NOT_FOUND)
            if job.status != ExportStatus.DONE or job.storage_key is None or job.file_name is None:
                raise ProblemError(
                    ProblemType.CONFLICT,
                    detail="This export is not ready, or it has expired. Ask for a new one.",
                    extensions={"code": "export.not_available"},
                )
            if job.expires_at is not None and job.expires_at <= now:
                raise ProblemError(
                    ProblemType.CONFLICT,
                    detail="This export has expired. Ask for a new one.",
                    extensions={"code": "export.expired"},
                )
            job.downloaded_at = now
            await self._audit.record(
                session,
                AuditEvent(
                    action="export.downloaded",
                    actor=actor.audit_actor(),
                    context=context,
                    permission_used=SPECS[ExportType(job.export_type)].permission,
                    target_type="export_job",
                    target_id=job.id,
                    changes={"export_type": ValueChange(None, job.export_type)},
                ),
            )
            try:
                url = await self._store.download_url(
                    job.storage_key, filename=job.file_name, content_type="text/csv"
                )
            except FileStoreError as exc:
                raise ProblemError(ProblemType.TEMPORARILY_UNAVAILABLE) from exc
        return url, DOWNLOAD_SECONDS
