"""Shared rate limiting with ephemeral counters in Redis (docs/architecture.md §3.1, ADR-010).

A sliding log per key: a sorted set of request timestamps, trimmed to the window on every
call. A key expires one window after its last entry, so no key outlives its window.

Key names never contain the subject itself (IP address, email, user ID): the subject is
HMAC-ed with a dedicated key. When Redis cannot be reached, each scope behaves as defined in
the architecture: authentication, download and export limits fail closed with 503; general
API limits fail open with a warning for operational alerting.

Limits and windows are not defined here. Callers build RateLimitRule values from the
security settings they belong to.
"""

import hashlib
import hmac
import logging
import math
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from app.platform.clock import Clock
from app.platform.errors import ProblemError, ProblemType

logger = logging.getLogger(__name__)

MAX_WINDOW: Final = timedelta(hours=1)

# How long a client is told to wait when a fail-closed limit cannot be checked.
STORE_UNAVAILABLE_RETRY_AFTER_SECONDS: Final = 30

# Connection and command timeouts. A rate-limit check must not hold up a request for long,
# and the fail-open/fail-closed decision must happen quickly when Redis is down.
REDIS_TIMEOUT_SECONDS: Final = 0.5

_RULE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")


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
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
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


# KEYS[1]: key. ARGV: now_ms, window_ms, limit, member, mode.
# mode "peek" only reads; "hit" adds an entry if under the limit; "record" always adds one.
# Returns {allowed (1/0), count after the call, retry_after_ms}. "allowed" reflects the
# count before the call. retry_after_ms is when the count drops below the limit again.
_SLIDING_LOG: Final = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local mode = ARGV[5]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
local allowed = count < limit
local retry_after = 0
if not allowed then
  local entry = redis.call('ZRANGE', key, count - limit, count - limit, 'WITHSCORES')
  retry_after = tonumber(entry[2]) + window - now
end
if mode == 'record' or (mode == 'hit' and allowed) then
  redis.call('ZADD', key, now, ARGV[4])
  redis.call('PEXPIRE', key, window)
  count = count + 1
end
return {allowed and 1 or 0, count, retry_after}
"""


def create_redis_client(url: str) -> Redis:
    return Redis.from_url(
        url,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
        decode_responses=False,
    )


class RateLimiter:
    def __init__(self, redis: Redis, *, hmac_key: bytes, clock: Clock) -> None:
        self._redis = redis
        self._hmac_key = hmac_key
        self._clock = clock
        self._script = redis.register_script(_SLIDING_LOG)

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
        now_ms = int(self._clock.now().timestamp() * 1000)
        window_ms = int(rule.window.total_seconds() * 1000)
        member = f"{now_ms}:{secrets.token_hex(8)}"
        try:
            allowed, count, retry_after_ms = await self._script(
                keys=[self.key_for(rule, subject)],
                args=[now_ms, window_ms, rule.limit, member, mode],
            )
        except RedisError as exc:
            return self._store_unavailable(rule, exc)
        return Decision(
            allowed=bool(allowed),
            remaining=max(rule.limit - int(count), 0),
            retry_after_seconds=max(math.ceil(int(retry_after_ms) / 1000), 1) if not allowed else 0,
        )

    @staticmethod
    def _store_unavailable(rule: RateLimitRule, exc: RedisError) -> Decision:
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
