"""Problem details, request context middleware and client address resolution."""

import ipaddress
import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from app.platform.config import IPNetwork
from app.platform.errors import PROBLEM_MEDIA_TYPE, ProblemError, ProblemType, install_error_handlers
from app.platform.middleware import SECURITY_HEADERS, RequestContextMiddleware, resolve_client_ip

TRUSTED: tuple[IPNetwork, ...] = (ipaddress.ip_network("10.0.0.0/8"),)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str


def build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/conflict")
    async def conflict() -> None:
        raise ProblemError(ProblemType.CONFLICT, extensions={"code": "test.conflict"})

    @app.get("/limited")
    async def limited() -> None:
        raise ProblemError(ProblemType.RATE_LIMITED, headers={"Retry-After": "7"})

    @app.post("/items")
    async def create(body: Body) -> Body:
        return body

    @app.get("/tokens/{token}")
    async def token_route(token: str) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("unexpected failure with detail that must not leak")

    @app.get("/client")
    async def client(request: Request) -> dict[str, str | None]:
        ip = request.state.client_ip
        return {"ip": str(ip) if ip else None}

    app.add_middleware(RequestContextMiddleware, trusted_proxies=TRUSTED)
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(
        app=build_app(), raise_app_exceptions=False, client=("203.0.113.9", 50000)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def assert_problem(response: httpx.Response, status: int, type_: str) -> dict[str, object]:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, object] = response.json()
    assert body["type"] == type_
    assert body["status"] == status
    assert body["request_id"] == response.headers["x-request-id"]
    assert set(body) >= {"type", "title", "status", "detail", "instance", "request_id"}
    return body


async def test_problem_error_is_rendered_with_extensions(client: httpx.AsyncClient) -> None:
    body = assert_problem(await client.get("/conflict"), 409, "/problems/conflict")
    assert body["code"] == "test.conflict"
    assert body["instance"] == "/conflict"


async def test_rate_limited_problem_carries_retry_after(client: httpx.AsyncClient) -> None:
    response = await client.get("/limited")
    assert_problem(response, 429, "/problems/rate-limited")
    assert response.headers["retry-after"] == "7"


async def test_validation_errors_list_fields_without_echoing_input(client: httpx.AsyncClient) -> None:
    response = await client.post("/items", json={"name": 5, "secret_extra": "do-not-echo"})
    body = assert_problem(response, 422, "/problems/validation-error")
    fields = {error["field"] for error in body["errors"]}  # type: ignore[attr-defined]
    assert fields == {"name", "secret_extra"}
    assert "do-not-echo" not in response.text


async def test_unknown_route_is_not_found_problem(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get("/nope"), 404, "/problems/not-found")


async def test_wrong_method_is_about_blank_problem(client: httpx.AsyncClient) -> None:
    body = assert_problem(await client.delete("/conflict"), 405, "about:blank")
    assert body["title"] == "Method Not Allowed"


async def test_unhandled_error_is_internal_problem_with_headers(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.request"):
        response = await client.get("/boom")
    body = assert_problem(response, 500, "/problems/internal-error")
    assert "must not leak" not in response.text
    assert body["detail"] is None
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value
    assert any(record.message == "request.unhandled_error" for record in caplog.records)


async def test_security_headers_and_fresh_request_id(client: httpx.AsyncClient) -> None:
    first = await client.get("/tokens/abc", headers={"X-Request-ID": "forged"})
    second = await client.get("/tokens/abc")
    for name, value in SECURITY_HEADERS:
        assert first.headers[name] == value
    assert first.headers["x-request-id"] != "forged"
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_access_log_uses_route_template_not_raw_path(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        await client.get("/tokens/fixture-invite-token-value")
    records = [record for record in caplog.records if record.message == "http.request"]
    assert records
    assert records[-1].__dict__["route"] == "/tokens/{token}"
    assert records[-1].__dict__["status"] == 200
    assert "fixture-invite-token-value" not in json.dumps(
        [record.__dict__ for record in records], default=str
    )


async def test_client_ip_ignores_forwarded_for_from_untrusted_peer(client: httpx.AsyncClient) -> None:
    response = await client.get("/client", headers={"X-Forwarded-For": "198.51.100.1"})
    assert response.json() == {"ip": "203.0.113.9"}


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        ("203.0.113.9", [], "203.0.113.9"),
        ("203.0.113.9", ["198.51.100.1"], "203.0.113.9"),
        ("10.0.0.5", [], "10.0.0.5"),
        ("10.0.0.5", ["198.51.100.1"], "198.51.100.1"),
        ("10.0.0.5", ["1.1.1.1, 198.51.100.1, 10.0.0.7"], "198.51.100.1"),
        ("10.0.0.5", ["1.1.1.1", "198.51.100.1"], "198.51.100.1"),
        ("10.0.0.5", ["10.0.0.8, 10.0.0.7"], "10.0.0.8"),
        ("10.0.0.5", ["198.51.100.1, garbage"], "10.0.0.5"),
        ("10.0.0.5", ["2001:db8::1"], "2001:db8::1"),
        ("not-an-ip", [], None),
        (None, ["198.51.100.1"], None),
    ],
)
def test_resolve_client_ip(peer: str | None, forwarded: list[str], expected: str | None) -> None:
    result = resolve_client_ip(peer, forwarded, TRUSTED)
    assert (str(result) if result else None) == expected
