"""Opaque keyset cursors (docs/api-architecture.md §3, Pagination).

A cursor encodes the sort position of the last row returned, `(timestamp, id)`, in base64url.
It carries nothing a client could use beyond asking for the next page, and an invalid one is
a validation error, never a server error.
"""

import base64
import binascii
import uuid
from datetime import datetime

from app.platform.errors import ProblemError, ProblemType

type Position = tuple[datetime, uuid.UUID]


def encode_cursor(position: Position | None) -> str | None:
    if position is None:
        return None
    raw = f"{position[0].isoformat()}|{position[1]}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> Position | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        moment, _, identifier = raw.partition("|")
        position = (datetime.fromisoformat(moment), uuid.UUID(identifier))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise _invalid() from exc
    if position[0].tzinfo is None:
        raise _invalid()
    return position


def _invalid() -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={
            "errors": [
                {"field": "cursor", "code": "cursor.invalid", "message": "Start again from the first page."}
            ]
        },
    )
