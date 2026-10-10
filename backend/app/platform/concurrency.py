"""Optimistic concurrency for versioned records (docs/api-architecture.md §3).

Responses of versioned resources carry `ETag: "<version>"`; a PATCH must send the version it
was based on in `If-Match`. A missing or malformed header is a validation error, a stale one
`412 precondition-failed`, so a change made from an outdated screen is never applied.
"""

import re
from typing import Annotated, Final

from fastapi import Header

from app.platform.errors import ProblemError, ProblemType

_ETAG: Final = re.compile(r'^(?:W/)?"(\d{1,9})"$')

IfMatch = Annotated[str | None, Header(alias="If-Match", max_length=20)]


def etag(version: int) -> str:
    return f'"{version}"'


def expected_version(if_match: str | None) -> int:
    match = _ETAG.fullmatch(if_match.strip()) if if_match else None
    if match is None:
        raise ProblemError(
            ProblemType.VALIDATION_ERROR,
            extensions={
                "errors": [
                    {
                        "field": "If-Match",
                        "code": "if_match.required",
                        "message": "Reload the record, then make the change again.",
                    }
                ]
            },
        )
    return int(match.group(1))


def check_version(current: int, expected: int) -> None:
    if current != expected:
        raise ProblemError(ProblemType.PRECONDITION_FAILED)
