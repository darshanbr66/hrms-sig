from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.main import create_app
from app.platform.config import ApiSettings
from app.platform.jobs import JobHealth
from tests.api_support import api_settings
from tests.conftest import MongoServer


def settings_for(mongodb_uri: SecretStr) -> ApiSettings:
    return api_settings(mongodb_uri)


async def client_for(settings: ApiSettings, app: FastAPI | None = None) -> AsyncIterator[httpx.AsyncClient]:
    app = app or create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture
async def client(migrated_mongo: MongoServer) -> AsyncIterator[httpx.AsyncClient]:
    async for http in client_for(settings_for(migrated_mongo.uri("hrms_app"))):
        yield http


@pytest.fixture
async def client_without_database() -> AsyncIterator[httpx.AsyncClient]:
    # A name that never resolves, so no socket is ever opened (see test_ratelimit.py).
    url = SecretStr(
        "mongodb://hrms_app:pw@hrms-test-database.invalid:27017/"
        "?directConnection=true&serverSelectionTimeoutMS=500"
    )
    async for http in client_for(settings_for(url)):
        yield http


async def test_live(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_when_database_reachable(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_not_ready_without_database(client_without_database: httpx.AsyncClient) -> None:
    response = await client_without_database.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["type"] == "/problems/temporarily-unavailable"
    assert response.headers["retry-after"] == "5"
    assert "hrms_app" not in response.text


async def test_docs_are_disabled_by_default(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/docs")).status_code == 404
    assert (await client.get("/api/v1/openapi.json")).status_code == 404


class Ticks:
    """A monotonic clock moved by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_readiness_follows_the_jobs_running_in_the_api(migrated_mongo: MongoServer) -> None:
    settings = settings_for(migrated_mongo.uri("hrms_app"))
    app = create_app(settings)
    ticks = Ticks()
    health = JobHealth(stale_after=timedelta(minutes=16), monotonic=ticks)
    async for client in client_for(settings, app):
        app.state.job_health = health
        # Not started yet: no heartbeat, not ready.
        assert (await client.get("/api/health/ready")).status_code == 503
        health.beat()
        assert (await client.get("/api/health/ready")).status_code == 200
        ticks.now += 15 * 60
        assert (await client.get("/api/health/ready")).status_code == 200
        # The scheduler has not ticked for longer than a lease: taken out of rotation.
        ticks.now += 2 * 60
        stale = await client.get("/api/health/ready")
        assert stale.status_code == 503
        assert stale.json()["type"] == "/problems/temporarily-unavailable"
        assert stale.headers["retry-after"] == "5"
        health.beat()
        assert (await client.get("/api/health/ready")).status_code == 200
        # A runner that ended unexpectedly stays failed, whatever beats after it.
        health.fail("RuntimeError")
        health.beat()
        failed = await client.get("/api/health/ready")
        assert failed.status_code == 503
        assert "RuntimeError" not in failed.text
        # Liveness is about the process, not the jobs.
        assert (await client.get("/api/health/live")).status_code == 200
