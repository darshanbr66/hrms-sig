"""Rate limiter against a real Redis configured with the production ACL (infra/redis)."""

import asyncio
import logging
import secrets
import socket
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from redis.exceptions import AuthenticationError, NoPermissionError, ResponseError

from app.platform.errors import ProblemType
from app.platform.ratelimit import (
    MAX_WINDOW,
    STORE_UNAVAILABLE_RETRY_AFTER_SECONDS,
    FailureMode,
    RateLimiter,
    RateLimitExceeded,
    RateLimitRule,
    RateLimitScope,
    RateLimitStoreUnavailable,
    create_redis_client,
)
from tests.conftest import RedisServer

HMAC_KEY = b"test-hmac-key-for-rate-limit-keys"


class SteppingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def rule(
    scope: RateLimitScope = RateLimitScope.AUTH, limit: int = 3, window_seconds: int = 60
) -> RateLimitRule:
    return RateLimitRule(
        scope=scope,
        name=f"test-{secrets.token_hex(3)}",
        limit=limit,
        window=timedelta(seconds=window_seconds),
    )


@pytest.fixture
async def redis_client(redis: RedisServer) -> AsyncIterator[aioredis.Redis]:
    client = create_redis_client(redis.url())
    yield client
    await client.aclose()


@pytest.fixture
def clock() -> SteppingClock:
    return SteppingClock()


@pytest.fixture
def limiter(redis_client: aioredis.Redis, clock: SteppingClock) -> RateLimiter:
    return RateLimiter(redis_client, hmac_key=HMAC_KEY, clock=clock)


async def test_hit_allows_up_to_the_limit_then_denies(limiter: RateLimiter) -> None:
    limit_rule = rule(limit=3)
    decisions = [await limiter.hit(limit_rule, "198.51.100.1") for _ in range(4)]
    assert [decision.allowed for decision in decisions] == [True, True, True, False]
    assert [decision.remaining for decision in decisions[:3]] == [2, 1, 0]
    assert decisions[3].retry_after_seconds == 60


async def test_subjects_are_counted_separately(limiter: RateLimiter) -> None:
    limit_rule = rule(limit=1)
    assert (await limiter.hit(limit_rule, "198.51.100.1")).allowed
    assert (await limiter.hit(limit_rule, "198.51.100.2")).allowed
    assert not (await limiter.hit(limit_rule, "198.51.100.1")).allowed


async def test_window_slides(limiter: RateLimiter, clock: SteppingClock) -> None:
    limit_rule = rule(limit=2, window_seconds=60)
    await limiter.hit(limit_rule, "s")
    clock.advance(timedelta(seconds=30))
    await limiter.hit(limit_rule, "s")
    clock.advance(timedelta(seconds=10))
    denied = await limiter.hit(limit_rule, "s")
    assert not denied.allowed
    assert denied.retry_after_seconds == 20  # the first entry leaves the window at t=60
    clock.advance(timedelta(seconds=21))
    assert (await limiter.hit(limit_rule, "s")).allowed
    assert not (await limiter.hit(limit_rule, "s")).allowed


async def test_peek_does_not_count_and_record_always_counts(limiter: RateLimiter) -> None:
    limit_rule = rule(limit=2)
    for _ in range(5):
        assert (await limiter.peek(limit_rule, "s")).allowed
    await limiter.record(limit_rule, "s")
    await limiter.record(limit_rule, "s")
    third = await limiter.record(limit_rule, "s")
    assert not third.allowed
    assert not (await limiter.peek(limit_rule, "s")).allowed


async def test_enforce_raises_rate_limited_problem(limiter: RateLimiter) -> None:
    limit_rule = rule(limit=1)
    await limiter.enforce(limit_rule, "s")
    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.enforce(limit_rule, "s")
    assert raised.value.problem_type is ProblemType.RATE_LIMITED
    assert raised.value.headers == {"Retry-After": "60"}


async def test_keys_hold_no_subject(limiter: RateLimiter, redis_client: aioredis.Redis) -> None:
    limit_rule = rule()
    subject = "person@example.test"
    key = limiter.key_for(limit_rule, subject)
    assert subject not in key
    assert key.startswith(f"rl:auth:{limit_rule.name}:")
    assert key != RateLimiter(redis_client, hmac_key=b"another-key", clock=SteppingClock()).key_for(
        limit_rule, subject
    )


async def test_keys_expire_one_window_after_the_last_entry(
    limiter: RateLimiter, redis_client: aioredis.Redis
) -> None:
    limit_rule = rule(window_seconds=1)
    await limiter.hit(limit_rule, "s")
    key = limiter.key_for(limit_rule, "s")
    assert await redis_client.zcard(key) == 1
    await asyncio.sleep(1.3)
    assert await redis_client.zcard(key) == 0


@pytest.mark.parametrize(
    "commands",
    [
        ("SET", "rl:x", "1"),
        ("GET", "rl:x"),
        ("KEYS", "*"),
        ("FLUSHALL",),
        ("CONFIG", "GET", "save"),
        ("ZADD", "other:x", "1", "m"),
        ("EVAL", "return 1", "0"),
    ],
)
async def test_acl_user_is_limited_to_rate_limit_commands_and_keys(
    redis_client: aioredis.Redis, commands: tuple[str, ...]
) -> None:
    with pytest.raises((NoPermissionError, ResponseError)):
        await redis_client.execute_command(*commands)  # type: ignore[no-untyped-call]


async def test_default_user_is_disabled(redis: RedisServer) -> None:
    client = aioredis.Redis(host=redis.host, port=redis.port, socket_timeout=1)
    try:
        with pytest.raises(AuthenticationError):
            await client.ping()
    finally:
        await client.aclose()


def test_server_does_not_run_as_root(redis: RedisServer) -> None:
    result = redis.container.exec(["cat", "/proc/1/status"])
    uid_line = next(line for line in result.output.decode().splitlines() if line.startswith("Uid:"))
    # Real, effective, saved and filesystem UIDs of the server process.
    assert "0" not in uid_line.split()[1:]


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.fixture
async def unreachable_limiter(clock: SteppingClock) -> AsyncIterator[RateLimiter]:
    client = create_redis_client(f"redis://hrms_ratelimit:pw@127.0.0.1:{closed_port()}/0")
    yield RateLimiter(client, hmac_key=HMAC_KEY, clock=clock)
    await client.aclose()


@pytest.mark.parametrize("scope", [RateLimitScope.AUTH, RateLimitScope.DOWNLOAD, RateLimitScope.EXPORT])
async def test_store_down_fails_closed_for_protective_scopes(
    unreachable_limiter: RateLimiter, scope: RateLimitScope, caplog: pytest.LogCaptureFixture
) -> None:
    assert scope.failure_mode is FailureMode.CLOSED
    started = time.perf_counter()
    with (
        caplog.at_level(logging.WARNING, logger="app.platform.ratelimit"),
        pytest.raises(RateLimitStoreUnavailable) as raised,
    ):
        await unreachable_limiter.hit(rule(scope=scope), "198.51.100.1")
    assert time.perf_counter() - started < 2
    assert raised.value.problem_type is ProblemType.TEMPORARILY_UNAVAILABLE
    assert raised.value.headers == {"Retry-After": str(STORE_UNAVAILABLE_RETRY_AFTER_SECONDS)}
    assert [record.message for record in caplog.records] == ["ratelimit.store_unavailable"]


async def test_store_down_fails_open_for_general_api_limits(
    unreachable_limiter: RateLimiter, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.platform.ratelimit"):
        decision = await unreachable_limiter.hit(rule(scope=RateLimitScope.API), "user-id")
    assert decision.allowed
    assert decision.degraded
    [record] = caplog.records
    assert record.message == "ratelimit.store_unavailable"
    assert record.__dict__["failure_mode"] == "open"


@pytest.mark.parametrize(
    ("name", "limit", "window"),
    [
        ("Upper", 1, timedelta(seconds=10)),
        ("ok", 0, timedelta(seconds=10)),
        ("ok", 1, timedelta(milliseconds=500)),
        ("ok", 1, MAX_WINDOW + timedelta(seconds=1)),
    ],
)
def test_invalid_rules_are_refused(name: str, limit: int, window: timedelta) -> None:
    with pytest.raises(ValueError, match=r"name|limit|window"):
        RateLimitRule(scope=RateLimitScope.AUTH, name=name, limit=limit, window=window)
