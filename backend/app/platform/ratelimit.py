"""Shared rate limiting with counters in MongoDB (docs/architecture.md §3.1, ADR-030).

A sliding log per key: one document holding the request times (milliseconds) inside the
window, trimmed and extended by a single atomic update, so concurrent requests on several API
instances count exactly. The log never holds more entries than the rule's limit, and the
document expires (TTL index on `expires_at`) one window after its last entry, so no counter
outlives its window and storage stays bounded.

Counters are written outside the request's transaction: an attempt counts even when the
request then fails, as a failed sign-in must.

Key names never contain the subject itself (IP address, email, user ID): the subject is
HMAC-ed with a dedicated key. Counter documents hold only that key, times and the expiry: no
password, token or personal data. When the database cannot be reached, each scope behaves as
defined in the architecture: authentication, download and export limits fail closed with 503;
general API limits fail open with a warning for operational alerting. Account lockout is not
a counter here: it is part of the account (identity module), so it survives anything.

Limits and windows are not defined here. Callers build RateLimitRule values from the
security settings they belong to.
"""

import dataclasses
import hashlib
import hmac
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Final

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.platform.clock import Clock
from app.platform.db import ID, Database, Document, Index, asc
from app.platform.errors import ProblemError, ProblemType

logger = logging.getLogger(__name__)

MAX_WINDOW: Final = timedelta(hours=1)

# How long a client is told to wait when a fail-closed limit cannot be checked.
STORE_UNAVAILABLE_RETRY_AFTER_SECONDS: Final = 30

_RULE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")


@dataclasses.dataclass(kw_only=True, eq=False)
class RateLimitCounter(Document):
    """`rate_limit_counters`: one sliding log. Not owned by an organization: the key is an HMAC
    of the subject, and a network address is not an organization's."""

    __collection__ = "rate_limit_counters"
    __id_field__ = "key"
    __tenant_scoped__ = False
    __indexes__ = (
        Index(asc("expires_at"), name="ttl_rate_limit_counters_expires_at", expire_after_seconds=0),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "key": {"bsonType": "string", "maxLength": 120},
        "hits": {"bsonType": "array", "maxItems": 1000, "items": {"bsonType": ["long", "int"]}},
    }

    key: str
    hits: list[int]
    allowed: bool
    expires_at: datetime


class FailureMode(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class RateLimitScope(StrEnum):
    AUTH = "auth"
    API = "api"
    DOWNLOAD = "dl"
    EXPORT = "exp"

    @property
    def failure_mode(self) -> FailureMode:
        return FailureMode.OPEN if self is RateLimitScope.API else FailureMode.CLOSED


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    scope: RateLimitScope
    name: str
    limit: int
    window: timedelta

    def __post_init__(self) -> None:
        if not _RULE_NAME.fullmatch(self.name):
            raise ValueError("rule name must be lowercase letters, digits, '_', '.' or '-'")
        if not 1 <= self.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not timedelta(seconds=1) <= self.window <= MAX_WINDOW:
            raise ValueError("window must be between 1 second and 1 hour")


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    degraded: bool = False


class RateLimitExceeded(ProblemError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(ProblemType.RATE_LIMITED, headers={"Retry-After": str(retry_after_seconds)})


class RateLimitStoreUnavailable(ProblemError):
    def __init__(self) -> None:
        super().__init__(
            ProblemType.TEMPORARILY_UNAVAILABLE,
            headers={"Retry-After": str(STORE_UNAVAILABLE_RETRY_AFTER_SECONDS)},
        )


def _sliding_log(
    now_ms: int, window_ms: int, limit: int, mode: str, expires_at: datetime
) -> list[dict[str, Any]]:
    """The atomic update: drop entries outside the window, decide, then (by mode) add this one.
    "hit" adds it only when under the limit; "record" always adds it. The log keeps at most
    `limit` entries, the newest."""
    live = {
        "$filter": {
            "input": {"$ifNull": ["$hits", []]},
            "as": "h",
            "cond": {"$gt": ["$$h", now_ms - window_ms]},
        }
    }
    allowed = {"$lt": [{"$size": "$hits"}, limit]}
    add = True if mode == "record" else "$allowed"
    return [
        {"$set": {"hits": live}},
        {"$set": {"allowed": allowed}},
        {
            "$set": {
                "hits": {
                    "$cond": [add, {"$slice": [{"$concatArrays": ["$hits", [now_ms]]}, -limit]}, "$hits"]
                },
                "expires_at": expires_at,
            }
        },
    ]


class RateLimiter:
    def __init__(self, database: Database, *, hmac_key: bytes, clock: Clock) -> None:
        self._collection = database.collection(RateLimitCounter.__collection__)
        self._hmac_key = hmac_key
        self._clock = clock

    def key_for(self, rule: RateLimitRule, subject: str) -> str:
        digest = hmac.new(self._hmac_key, subject.encode(), hashlib.sha256).hexdigest()
        return f"rl:{rule.scope.value}:{rule.name}:{digest}"

    async def peek(self, rule: RateLimitRule, subject: str) -> Decision:
        """Check the limit without counting this call."""
        return await self._run(rule, subject, "peek")

    async def hit(self, rule: RateLimitRule, subject: str) -> Decision:
        """Count this call if it is within the limit."""
        return await self._run(rule, subject, "hit")

    async def record(self, rule: RateLimitRule, subject: str) -> Decision:
        """Count an event unconditionally, for example a failed sign-in attempt."""
        return await self._run(rule, subject, "record")

    async def enforce(self, rule: RateLimitRule, subject: str) -> Decision:
        """Count this call, raising a 429 problem when it exceeds the limit."""
        decision = await self.hit(rule, subject)
        if not decision.allowed:
            raise RateLimitExceeded(decision.retry_after_seconds)
        return decision

    async def _run(self, rule: RateLimitRule, subject: str, mode: str) -> Decision:
        now = self._clock.now()
        now_ms = int(now.timestamp() * 1000)
        window_ms = int(rule.window.total_seconds() * 1000)
        key = self.key_for(rule, subject)
        try:
            if mode == "peek":
                stored = await self._collection.find_one({ID: key})
                hits = [hit for hit in (stored or {}).get("hits", []) if hit > now_ms - window_ms]
                allowed = len(hits) < rule.limit
                count = len(hits)
            else:
                stored = await self._collection.find_one_and_update(
                    {ID: key},
                    _sliding_log(now_ms, window_ms, rule.limit, mode, now + rule.window),
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
                hits = list(stored["hits"]) if stored else []
                allowed = bool(stored and stored["allowed"])
                count = len(hits)
        except PyMongoError as exc:
            return self._store_unavailable(rule, exc)
        if allowed:
            return Decision(allowed=True, remaining=max(rule.limit - count, 0), retry_after_seconds=0)
        oldest_relevant = sorted(hits)[-rule.limit] if len(hits) >= rule.limit else now_ms
        retry_after_ms = oldest_relevant + window_ms - now_ms
        return Decision(
            allowed=False,
            remaining=max(rule.limit - count, 0),
            retry_after_seconds=max(math.ceil(retry_after_ms / 1000), 1),
        )

    @staticmethod
    def _store_unavailable(rule: RateLimitRule, exc: PyMongoError) -> Decision:
        mode = rule.scope.failure_mode
        logger.warning(
            "ratelimit.store_unavailable",
            extra={
                "scope": rule.scope.value,
                "rule": rule.name,
                "failure_mode": mode.value,
                "error_type": type(exc).__name__,
            },
        )
        if mode is FailureMode.CLOSED:
            raise RateLimitStoreUnavailable from exc
        return Decision(allowed=True, remaining=0, retry_after_seconds=0, degraded=True)
