"""Cross-site request forgery protection (docs/security-architecture.md §5).

Session cookies are SameSite, and in addition every unsafe request (POST, PUT, PATCH,
DELETE) must carry `X-Requested-With: sv-web`, a header a cross-site form or a simple
cross-origin request cannot set, and, when the browser sends an Origin header, it must be the
application's own origin. The SPA and the API share one origin, so there is no CORS.
"""

import json
from typing import Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.platform.errors import (
    PROBLEM_MEDIA_TYPE,
    PROBLEM_TYPE_PREFIX,
    ProblemType,
    problem_body,
    problem_status,
)

CSRF_HEADER: Final = "x-requested-with"
CSRF_HEADER_VALUE: Final = "sv-web"
UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CsrfMiddleware:
    def __init__(self, app: ASGIApp, *, app_origin: str) -> None:
        self.app = app
        self.app_origin = app_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS and not self._allowed(scope):
            await self._refuse(scope["path"], send)
            return
        await self.app(scope, receive, send)

    def _allowed(self, scope: Scope) -> bool:
        headers = Headers(scope=scope)
        if headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
            return False
        origin = headers.get("origin")
        return origin is None or origin == self.app_origin

    @staticmethod
    async def _refuse(path: str, send: Send) -> None:
        status = problem_status(ProblemType.FORBIDDEN)
        body = problem_body(
            type_=PROBLEM_TYPE_PREFIX + ProblemType.FORBIDDEN.value,
            status=status,
            title="This request was blocked because it did not come from the HRMS application.",
            instance=path,
            extensions={"code": "csrf"},
        )
        encoded = json.dumps(body).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", PROBLEM_MEDIA_TYPE.encode()),
                    (b"content-length", str(len(encoded)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": encoded})
