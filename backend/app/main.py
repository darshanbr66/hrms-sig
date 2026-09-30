"""API application factory.

Run from backend/ with:
    uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop

psycopg's async driver needs a selector event loop; that is the Linux default and must be
requested explicitly on Windows, where uvicorn otherwise picks the Proactor loop.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.platform import health
from app.platform.config import ApiSettings
from app.platform.db import Database, create_engine
from app.platform.errors import install_error_handlers
from app.platform.logging import configure_logging
from app.platform.middleware import RequestContextMiddleware

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
    return app


def create_app(settings: ApiSettings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(create_engine(settings.database_url_app, application_name="hrms-api"))
        app.state.database = database
        try:
            yield
        finally:
            await database.dispose()

    app = _build(docs_enabled=settings.api_docs_enabled, lifespan=lifespan)
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
