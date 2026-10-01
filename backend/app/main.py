"""API application factory.

Run from backend/ with:
    uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop

psycopg's async driver needs a selector event loop; that is the Linux default and must be
requested explicitly on Windows, where uvicorn otherwise picks the Proactor loop.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from app.modules.access import router as access_router
from app.modules.access.public import SessionActorProvider
from app.modules.access.service import AccessService
from app.modules.audit import router as audit_router
from app.modules.identity import router as identity_router
from app.modules.identity.service import IdentityService, Sleep
from app.modules.people import public as people
from app.platform import health
from app.platform.audit.writer import AuditWriter
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock, SystemClock
from app.platform.config import ApiSettings
from app.platform.csrf import CsrfMiddleware
from app.platform.db import Database, create_engine
from app.platform.errors import install_error_handlers
from app.platform.logging import configure_logging
from app.platform.middleware import RequestContextMiddleware
from app.platform.ratelimit import RateLimiter, create_redis_client
from app.platform.security.crypto import FieldCipher
from app.platform.security.passwords import BreachedPasswordChecker

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
    app.include_router(identity_router.router)
    app.include_router(access_router.router)
    app.include_router(audit_router.router)
    return app


def create_app(settings: ApiSettings, *, clock: Clock | None = None, sleep: Sleep | None = None) -> FastAPI:
    """The API. `clock` and `sleep` are injectable for tests (time-dependent security rules)."""
    app_clock: Clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(create_engine(settings.database_url_app, application_name="hrms-api"))
        redis = create_redis_client(settings.redis_url.get_secret_value())
        breach_client = httpx.AsyncClient() if settings.hibp_enabled else None
        audit = AuditWriter(app_clock)
        authorizer = Authorizer(people.relationships, app_clock)
        app.state.clock = app_clock
        app.state.database = database
        app.state.audit_writer = audit
        app.state.authorizer = authorizer
        app.state.actor_provider = SessionActorProvider(database, app_clock)
        app.state.identity_service = IdentityService(
            database=database,
            clock=app_clock,
            cipher=FieldCipher(settings.field_keys, settings.field_encryption_active_version),
            breach_checker=BreachedPasswordChecker(hibp_enabled=settings.hibp_enabled, client=breach_client),
            rate_limiter=RateLimiter(redis, hmac_key=settings.rate_limit_hmac_key_bytes, clock=app_clock),
            audit=audit,
            authorizer=authorizer,
            sleep=sleep or asyncio.sleep,
        )
        app.state.access_service = AccessService(
            database=database, clock=app_clock, audit=audit, authorizer=authorizer
        )
        try:
            yield
        finally:
            if breach_client is not None:
                await breach_client.aclose()
            await redis.aclose()
            await database.dispose()

    app = _build(docs_enabled=settings.api_docs_enabled, lifespan=lifespan)
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
    return create_app(settings)


def openapi_document() -> dict[str, Any]:
    """The API contract, independent of any environment's settings."""
    return _build(docs_enabled=False).openapi()
