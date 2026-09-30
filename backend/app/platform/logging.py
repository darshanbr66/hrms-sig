"""Structured JSON logging with redaction (docs/security-architecture.md §7).

Log records carry an event name as the message and IDs in `extra`. Any `extra` key whose
name contains a sensitive word is replaced before the record is written, at any depth.
"""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

REDACTED = "[redacted]"

# Matched against each underscore/dash/dot-separated part of a key, so "access_token" and
# "totp_code" are redacted while "status" or "company" are not.
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "password",
        "token",
        "secret",
        "otp",
        "code",
        "authorization",
        "cookie",
        "iban",
        "account",
        "pan",
        "aadhaar",
        "salary",
        "amount",
    }
)
_KEY_SPLIT = re.compile(r"[_\-.\s]+")

_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime", "taskName"}
)


def is_sensitive_key(key: str) -> bool:
    return any(part in _SENSITIVE_KEY_PARTS for part in _KEY_SPLIT.split(key.lower()))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and is_sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS and key not in payload
        }
        payload.update(redact(extras))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn's own handlers are replaced by the root handler. Its access log is disabled
    # because it prints raw paths, which can contain tokens; RequestContextMiddleware logs
    # the matched route template instead.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
