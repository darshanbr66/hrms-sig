import base64
import socket
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from app.main import create_app
from app.platform.config import ApiSettings, AppEnv
from tests.conftest import PostgresServer


def settings_for(database_url: SecretStr) -> ApiSettings:
    return ApiSettings(
        app_env=AppEnv.TEST,
        database_url_app=database_url,
        redis_url=SecretStr("redis://hrms_ratelimit:unused@127.0.0.1:1/0"),
        rate_limit_key_hmac_key=SecretStr(base64.b64encode(b"k" * 32).decode()),
    )


async def client_for(settings: ApiSettings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture
async def client(migrated_postgres: PostgresServer) -> AsyncIterator[httpx.AsyncClient]:
    async for http in client_for(settings_for(migrated_postgres.url("hrms_app"))):
        yield http


@pytest.fixture
async def client_without_database() -> AsyncIterator[httpx.AsyncClient]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = SecretStr(f"postgresql://hrms_app:pw@127.0.0.1:{port}/hrms?connect_timeout=1")
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
