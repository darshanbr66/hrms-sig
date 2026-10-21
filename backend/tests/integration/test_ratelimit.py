"""Rate limiter against a real MongoDB, connected as the API's database user."""

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import AsyncMongoClient

from app.platform.db import CODEC_OPTIONS, ID, Database
from app.platform.errors import ProblemType
from app.platform.ratelimit import (
    MAX_WINDOW,
    STORE_UNAVAILABLE_RETRY_AFTER_SECONDS,
    FailureMode,
    RateLimitCounter,
    RateLimiter,
    RateLimitExceeded,
    RateLimitRule,
    RateLimitScope,
    RateLimitStoreUnavailable,
)
from tests.conftest import MongoServer, database_as

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
async def database(migrated_mongo: MongoServer) -> AsyncIterator[Database]:
    async with database_as(migrated_mongo, "hrms_app") as connected:
        yield connected


@pytest.fixture
def clock() -> SteppingClock:
    return SteppingClock()


@pytest.fixture
def limiter(database: Database, clock: SteppingClock) -> RateLimiter:
    return RateLimiter(database, hmac_key=HMAC_KEY, clock=clock)


async def stored(
    database: Database, limiter: RateLimiter, limit_rule: RateLimitRule, subject: str
) -> dict[str, object]:
    found = await database.collection(RateLimitCounter.__collection__).find_one(
        {ID: limiter.key_for(limit_rule, subject)}
    )
    assert found is not None
    return found


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


async def test_concurrent_hits_are_all_counted(limiter: RateLimiter) -> None:
    """The update is atomic: twenty simultaneous requests never let more than the limit through."""
    limit_rule = rule(limit=5)
    decisions = await asyncio.gather(*(limiter.hit(limit_rule, "s") for _ in range(20)))
    assert sum(decision.allowed for decision in decisions) == 5


async def test_keys_hold_no_subject_and_documents_hold_no_data(
    limiter: RateLimiter, database: Database, clock: SteppingClock
) -> None:
    limit_rule = rule()
    subject = "person@example.test"
    key = limiter.key_for(limit_rule, subject)
    assert subject not in key
    assert key.startswith(f"rl:auth:{limit_rule.name}:")
    assert key != RateLimiter(database, hmac_key=b"another-key", clock=SteppingClock()).key_for(
        limit_rule, subject
    )
    await limiter.hit(limit_rule, subject)
    document = await stored(database, limiter, limit_rule, subject)
    assert set(document) == {ID, "hits", "allowed", "expires_at"}
    assert subject not in str(document)


async def test_counters_are_bounded_and_expire_one_window_after_the_last_entry(
    limiter: RateLimiter, database: Database, clock: SteppingClock
) -> None:
    limit_rule = rule(limit=3, window_seconds=60)
    for _ in range(10):
        await limiter.record(limit_rule, "s")
        clock.advance(timedelta(seconds=1))
    document = await stored(database, limiter, limit_rule, "s")
    # The log never grows past the limit, however many attempts are recorded.
    assert len(document["hits"]) == 3  # type: ignore[arg-type]
    assert document["expires_at"] == clock.now() - timedelta(seconds=1) + limit_rule.window


async def test_expired_counters_are_removed_by_a_ttl_index(migrated_mongo: MongoServer) -> None:
    # Read as the migration user: the API's user may not even list indexes.
    async with database_as(migrated_mongo, "hrms_migrator") as migrator:
        cursor = await migrator.collection(RateLimitCounter.__collection__).list_indexes()
        indexes = {item["name"]: item async for item in cursor}
    ttl = indexes["ttl_rate_limit_counters_expires_at"]
    assert ttl["expireAfterSeconds"] == 0
    assert dict(ttl["key"]) == {"expires_at": 1}


# A name that never resolves (RFC 6761). Name resolution fails before any socket exists: a closed
# local port would leave PyMongo's monitor connecting when the client closes, and PyMongo 4.19
# does not close a socket whose connect is cancelled, so it would be reported later against
# whichever test the garbage collector runs in.
UNREACHABLE_URI = "mongodb://hrms_app:pw@hrms-test-database.invalid:27017/?directConnection=true"


@pytest.fixture
async def unreachable_limiter(clock: SteppingClock) -> AsyncIterator[RateLimiter]:
    client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient(
        UNREACHABLE_URI,
        serverSelectionTimeoutMS=300,
    )
    database = Database(client, "hrms")
    assert database.db.codec_options == CODEC_OPTIONS
    yield RateLimiter(database, hmac_key=HMAC_KEY, clock=clock)
    await client.close()


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
        ("ok", 1001, timedelta(seconds=10)),
        ("ok", 1, timedelta(milliseconds=500)),
        ("ok", 1, MAX_WINDOW + timedelta(seconds=1)),
    ],
)
def test_invalid_rules_are_refused(name: str, limit: int, window: timedelta) -> None:
    with pytest.raises(ValueError, match=r"name|limit|window"):
        RateLimitRule(scope=RateLimitScope.AUTH, name=name, limit=limit, window=window)
