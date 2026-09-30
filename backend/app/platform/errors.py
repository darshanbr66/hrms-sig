"""RFC 9457 problem details (docs/api-architecture.md §4).

Domain code raises ProblemError (or a subclass); the handlers below turn it, validation
errors and HTTP errors into application/problem+json. Unhandled exceptions are turned into
an internal-error problem by RequestContextMiddleware, which wraps the whole application.
"""

from collections.abc import Mapping
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.platform.logging import request_id_var

PROBLEM_MEDIA_TYPE = "application/problem+json"

# Problem types are relative URI references, so they are identical in every environment.
PROBLEM_TYPE_PREFIX = "/problems/"


class ProblemType(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    STEP_UP_REQUIRED = "step-up-required"
    MFA_ENROLMENT_REQUIRED = "mfa-enrolment-required"
    NOT_FOUND = "not-found"
    CONFLICT = "conflict"
    PRECONDITION_FAILED = "precondition-failed"
    VALIDATION_ERROR = "validation-error"
    RATE_LIMITED = "rate-limited"
    INTERNAL_ERROR = "internal-error"
    TEMPORARILY_UNAVAILABLE = "temporarily-unavailable"


_STATUS_AND_TITLE: dict[ProblemType, tuple[int, str]] = {
    ProblemType.UNAUTHENTICATED: (401, "Sign in to continue."),
    ProblemType.FORBIDDEN: (403, "You don't have access to this."),
    ProblemType.STEP_UP_REQUIRED: (403, "Enter a code from your authenticator app to continue."),
    ProblemType.MFA_ENROLMENT_REQUIRED: (403, "Set up an authenticator app to continue."),
    ProblemType.NOT_FOUND: (404, "We couldn't find what you asked for."),
    ProblemType.CONFLICT: (409, "This change conflicts with the current state of the record."),
    ProblemType.PRECONDITION_FAILED: (
        412,
        "This record changed after you opened it. Reload it and try again.",
    ),
    ProblemType.VALIDATION_ERROR: (422, "Some fields need attention."),
    ProblemType.RATE_LIMITED: (429, "Too many requests. Wait a moment, then try again."),
    ProblemType.INTERNAL_ERROR: (
        500,
        "We couldn't complete this request. Try again, and quote the request ID if it keeps happening.",
    ),
    ProblemType.TEMPORARILY_UNAVAILABLE: (503, "This service is temporarily unavailable. Try again shortly."),
}


def problem_status(problem_type: ProblemType) -> int:
    return _STATUS_AND_TITLE[problem_type][0]


class ProblemError(Exception):
    def __init__(
        self,
        problem_type: ProblemType,
        *,
        detail: str | None = None,
        headers: Mapping[str, str] | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(problem_type.value)
        self.problem_type = problem_type
        self.detail = detail
        self.headers = dict(headers or {})
        self.extensions = dict(extensions or {})


def problem_body(
    *,
    type_: str,
    status: int,
    title: str,
    instance: str,
    detail: str | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": type_,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id_var.get(),
    }
    body.update(extensions or {})
    return body


def internal_error_body(instance: str) -> dict[str, Any]:
    status, title = _STATUS_AND_TITLE[ProblemType.INTERNAL_ERROR]
    return problem_body(
        type_=PROBLEM_TYPE_PREFIX + ProblemType.INTERNAL_ERROR.value,
        status=status,
        title=title,
        instance=instance,
    )


def problem_response(
    request: Request,
    *,
    type_: str,
    status: int,
    title: str,
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> JSONResponse:
    body = problem_body(
        type_=type_,
        status=status,
        title=title,
        instance=request.url.path,
        detail=detail,
        extensions=extensions,
    )
    return JSONResponse(body, status_code=status, headers=dict(headers or {}), media_type=PROBLEM_MEDIA_TYPE)


def _from_problem_type(
    request: Request,
    problem_type: ProblemType,
    *,
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> JSONResponse:
    status, title = _STATUS_AND_TITLE[problem_type]
    return problem_response(
        request,
        type_=PROBLEM_TYPE_PREFIX + problem_type.value,
        status=status,
        title=title,
        detail=detail,
        headers=headers,
        extensions=extensions,
    )


async def _handle_problem(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ProblemError):
        raise exc
    return _from_problem_type(
        request, exc.problem_type, detail=exc.detail, headers=exc.headers, extensions=exc.extensions
    )


_LOCATION_ROOTS = frozenset({"body", "query", "path", "header", "cookie"})


async def _handle_validation(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ())]
        if location and location[0] in _LOCATION_ROOTS:
            location = location[1:]
        # The submitted value ("input") is deliberately not echoed back.
        errors.append(
            {"field": ".".join(location) or None, "code": error.get("type"), "message": error.get("msg")}
        )
    return _from_problem_type(request, ProblemType.VALIDATION_ERROR, extensions={"errors": errors})


async def _handle_http(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    if exc.status_code == 404:
        return _from_problem_type(request, ProblemType.NOT_FOUND)
    return problem_response(
        request,
        type_="about:blank",
        status=exc.status_code,
        title=HTTPStatus(exc.status_code).phrase,
        headers=exc.headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _handle_problem)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http)
