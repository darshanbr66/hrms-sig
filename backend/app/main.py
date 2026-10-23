"""API application factory.

Run from backend/ with:
    uv run --env-file ../.env uvicorn app.main:app_factory --factory

With `RUN_JOBS_IN_API=true` the API process also runs the worker's scheduled jobs (app/worker.py,
`JobRunner`), started and stopped with the application's lifespan (ADR-030). The API is then
ready only while the jobs are running (`/api/health/ready`), and if the jobs stop unexpectedly
the process shuts down so the platform restarts it.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from app.modules.access import router as access_router
from app.modules.access.grant_requests import GrantRequestService
from app.modules.access.public import AccountRoles, SessionActorProvider
from app.modules.access.service import AccessService
from app.modules.attendance import router as attendance_router
from app.modules.attendance.config import ConfigService as AttendanceConfigService
from app.modules.attendance.corrections import CorrectionService
from app.modules.attendance.locations import LocationService
from app.modules.attendance.service import AttendanceService
from app.modules.audit import router as audit_router
from app.modules.documents import router as documents_router
from app.modules.documents.service import DocumentService
from app.modules.erasure import router as erasure_router
from app.modules.erasure.service import ErasureService
from app.modules.identity import router as identity_router
from app.modules.identity.invitations import InvitationService
from app.modules.identity.password_reset import PasswordResetService
from app.modules.identity.service import IdentityService, Sleep
from app.modules.leave import router as leave_router
from app.modules.leave.holidays import HolidayService
from app.modules.leave.service import LeaveService
from app.modules.leave.year import LeaveYearService
from app.modules.notify import router as notify_router
from app.modules.notify.inbox import InboxService
from app.modules.org import router as org_router
from app.modules.org.service import OrgService
from app.modules.people import public as people
from app.modules.people import router as people_router
from app.modules.people.service import PeopleService
from app.modules.reports import router as reports_router
from app.modules.reports.service import ExportService
from app.modules.settings import router as settings_router
from app.modules.settings.service import SettingsService
from app.platform import files, health
from app.platform.audit.writer import AuditWriter
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock, SystemClock
from app.platform.config import ApiSettings, JobsInApiSettings
from app.platform.csrf import CsrfMiddleware
from app.platform.db import Database, create_client
from app.platform.errors import install_error_handlers
from app.platform.files import GridFsFileStore, LinkSigner
from app.platform.logging import configure_logging
from app.platform.middleware import RequestContextMiddleware
from app.platform.ratelimit import RateLimiter
from app.platform.security.crypto import FieldCipher
from app.platform.security.emails import EmailHasher
from app.platform.security.passwords import BreachedPasswordChecker
from app.worker import JobRunner, terminate_process

API_TITLE = "Sigvitas HRMS API"
API_VERSION = "1"
API_PREFIX = "/api/v1"
DOCS_URL = f"{API_PREFIX}/docs"
OPENAPI_URL = f"{API_PREFIX}/openapi.json"


def _build(*, docs_enabled: bool, lifespan: Any = None) -> FastAPI:
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        docs_url=DOCS_URL if docs_enabled else None,
        redoc_url=None,
        openapi_url=OPENAPI_URL if docs_enabled else None,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(files.router)
    app.include_router(identity_router.router)
    app.include_router(access_router.router)
    app.include_router(audit_router.router)
    app.include_router(settings_router.router)
    app.include_router(org_router.router)
    app.include_router(people_router.router)
    app.include_router(attendance_router.router)
    app.include_router(leave_router.router)
    app.include_router(leave_router.leave_router)
    app.include_router(notify_router.router)
    app.include_router(documents_router.router)
    app.include_router(reports_router.router)
    app.include_router(erasure_router.router)
    return app


type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def _with_jobs(lifespan: Lifespan, jobs: JobRunner) -> Lifespan:
    """The lifespan, with the jobs started once the services exist and stopped before the
    API's connections close. Readiness follows the jobs' health."""

    @asynccontextmanager
    async def run(app: FastAPI) -> AsyncIterator[None]:
        async with lifespan(app):
            app.state.job_health = jobs.health
            jobs.start()
            try:
                yield
            finally:
                await jobs.stop()

    return run


def create_app(
    settings: ApiSettings,
    *,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    jobs: JobRunner | None = None,
) -> FastAPI:
    """The API. `clock` and `sleep` are injectable for tests (time-dependent security rules);
    `jobs`, when given, runs the worker's scheduled jobs for as long as the application runs."""
    app_clock: Clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(
            create_client(settings.mongodb_uri, application_name="hrms-api"), settings.mongodb_database
        )
        breach_client = httpx.AsyncClient() if settings.hibp_enabled else None
        audit = AuditWriter(app_clock)
        authorizer = Authorizer(people.relationships, app_clock)
        email_hasher = EmailHasher(settings.email_lookup_key_bytes)
        breach_checker = BreachedPasswordChecker(hibp_enabled=settings.hibp_enabled, client=breach_client)
        limiter = RateLimiter(database, hmac_key=settings.rate_limit_hmac_key_bytes, clock=app_clock)
        app.state.clock = app_clock
        app.state.database = database
        app.state.audit_writer = audit
        app.state.authorizer = authorizer
        app.state.actor_provider = SessionActorProvider(database, app_clock)
        app.state.identity_service = IdentityService(
            database=database,
            clock=app_clock,
            cipher=FieldCipher(settings.field_keys, settings.field_encryption_active_version),
            breach_checker=breach_checker,
            rate_limiter=limiter,
            audit=audit,
            authorizer=authorizer,
            email_hasher=email_hasher,
            account_roles=AccountRoles(),
            sleep=sleep or asyncio.sleep,
        )
        app.state.password_reset_service = PasswordResetService(
            database=database,
            clock=app_clock,
            breach_checker=breach_checker,
            rate_limiter=limiter,
            audit=audit,
            email_hasher=email_hasher,
        )
        app.state.invitation_service = InvitationService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.settings_service = SettingsService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.org_service = OrgService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.people_service = PeopleService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.attendance_service = AttendanceService(
            database=database,
            clock=app_clock,
            authorizer=authorizer,
            rate_limiter=limiter,
            location_checks_enabled=settings.location_checks_enabled,
        )
        app.state.correction_service = CorrectionService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer, rate_limiter=limiter
        )
        app.state.location_service = LocationService(
            database=database,
            clock=app_clock,
            audit=audit,
            authorizer=authorizer,
            checks_enabled=settings.location_checks_enabled,
        )
        app.state.attendance_config_service = AttendanceConfigService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.holiday_service = HolidayService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.leave_service = LeaveService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer, rate_limiter=limiter
        )
        app.state.inbox_service = InboxService(database=database, clock=app_clock)
        link_signer = LinkSigner(settings.download_link_key_bytes)
        file_store = GridFsFileStore(database, link_signer)
        app.state.file_store = file_store
        app.state.link_signer = link_signer
        app.state.document_service = DocumentService(
            database=database,
            clock=app_clock,
            audit=audit,
            authorizer=authorizer,
            rate_limiter=limiter,
            store=file_store,
            uploads_enabled=settings.document_uploads_enabled,
        )
        app.state.export_service = ExportService(
            database=database,
            clock=app_clock,
            audit=audit,
            authorizer=authorizer,
            rate_limiter=limiter,
            store=file_store,
        )
        app.state.erasure_service = ErasureService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.leave_year_service = LeaveYearService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        grant_requests = GrantRequestService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        app.state.grant_request_service = grant_requests
        app.state.access_service = AccessService(
            database=database,
            clock=app_clock,
            audit=audit,
            authorizer=authorizer,
            grant_requests=grant_requests,
        )
        try:
            yield
        finally:
            if breach_client is not None:
                await breach_client.aclose()
            await database.dispose()

    app = _build(
        docs_enabled=settings.api_docs_enabled,
        lifespan=lifespan if jobs is None else _with_jobs(lifespan, jobs),
    )
    # Added first, so it runs inside RequestContextMiddleware: refusals get a request ID
    # and the security headers like every other response.
    app.add_middleware(CsrfMiddleware, app_origin=settings.app_base_url)
    app.add_middleware(
        RequestContextMiddleware,
        trusted_proxies=settings.trusted_proxy_cidrs,
        docs_paths=frozenset({DOCS_URL}) if settings.api_docs_enabled else frozenset(),
    )
    return app


def app_factory() -> FastAPI:
    settings = ApiSettings()  # values come from the environment
    configure_logging(settings.log_level)
    jobs = None
    if settings.run_jobs_in_api:
        job_settings = JobsInApiSettings()
        jobs = JobRunner(
            job_settings,
            database_uri=job_settings.jobs_mongodb_uri,
            application_name="hrms-api-jobs",
            on_unexpected_exit=terminate_process,
        )
    return create_app(settings, jobs=jobs)


def openapi_document() -> dict[str, Any]:
    """The API contract, independent of any environment's settings."""
    return _build(docs_enabled=False).openapi()
