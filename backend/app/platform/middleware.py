"""Request context: request ID, client address, security headers, access log, last-resort errors.

Implemented as plain ASGI middleware so it wraps every response, including errors.
"""

import ipaddress
import json
import logging
import time
import uuid
from collections.abc import Iterable, Sequence

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.config import IPNetwork
from app.platform.errors import PROBLEM_MEDIA_TYPE, internal_error_body
from app.platform.logging import request_id_var

logger = logging.getLogger("app.request")

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# docs/security-architecture.md §5. API responses are JSON, so the CSP forbids everything;
# the SPA's policy is set by the reverse proxy.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"),
    (
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)

# The interactive API docs page loads Swagger UI from its CDN. It exists only where
# API_DOCS_ENABLED is allowed (local and test), and only that page gets this policy.
DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _is_trusted(address: IPAddress, trusted: Sequence[IPNetwork]) -> bool:
    return any(address.version == network.version and address in network for network in trusted)


def resolve_client_ip(
    peer: str | None, forwarded_for: Iterable[str], trusted_proxies: Sequence[IPNetwork]
) -> IPAddress | None:
    """Return the client address, trusting X-Forwarded-For only across trusted proxies.

    The header is read right to left. The first address that is not a trusted proxy is the
    client. A malformed entry stops the walk at the last address known to be genuine.
    """
    if peer is None:
        return None
    try:
        address: IPAddress = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if not _is_trusted(address, trusted_proxies):
        return address
    hops = [hop.strip() for header in forwarded_for for hop in header.split(",")]
    for hop in reversed(hops):
        try:
            candidate = ipaddress.ip_address(hop)
        except ValueError:
            return address
        if not _is_trusted(candidate, trusted_proxies):
            return candidate
        address = candidate
    return address


class RequestContextMiddleware:
    def __init__(
        self, app: ASGIApp, *, trusted_proxies: Sequence[IPNetwork], docs_paths: frozenset[str] = frozenset()
    ) -> None:
        self.app = app
        self.trusted_proxies = tuple(trusted_proxies)
        self.docs_paths = docs_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Always generated here; an incoming X-Request-ID is never trusted or echoed.
        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        client = scope.get("client")
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["client_ip"] = resolve_client_ip(
            client[0] if client else None,
            Headers(scope=scope).getlist("x-forwarded-for"),
            self.trusted_proxies,
        )

        is_docs = scope["path"] in self.docs_paths
        status = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers[name] = value
                if is_docs:
                    headers["Content-Security-Policy"] = DOCS_CONTENT_SECURITY_POLICY
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            logger.exception("request.unhandled_error")
            if response_started:
                raise
            await self._send_internal_error(scope["path"], send_with_headers)
        finally:
            route = scope.get("route")
            logger.info(
                "http.request",
                extra={
                    "method": scope["method"],
                    # The route template, never the raw path: paths can carry tokens.
                    "route": getattr(route, "path", None),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)

    @staticmethod
    async def _send_internal_error(path: str, send: Send) -> None:
        body = json.dumps(internal_error_body(path)).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", PROBLEM_MEDIA_TYPE.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
