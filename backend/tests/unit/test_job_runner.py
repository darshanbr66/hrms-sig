"""Jobs inside the API process (RUN_JOBS_IN_API, ADR-030): one scheduler per process, restarted
after an unexpected error (also while it is being built), stopped with the API and cancelled
when it does not finish in time; its heartbeat says whether it runs, and a runner that ends
without being stopped is reported and stops the process."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest

import app.worker
from app.platform.jobs import JobHealth
from app.worker import JobRunner
from tests.unit.test_config import worker


class Escaped(BaseException):
    """An error that no `except Exception` catches, so the runner's task ends with it."""


class FakeScheduler:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.runs = 0
        self.builds = 0
        self.health: JobHealth | None = None

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.runs += 1
        if self.behaviour == "fail_once" and self.runs == 1:
            raise RuntimeError("unexpected")
        if self.behaviour == "hang":
            await asyncio.sleep(3600)  # a task that ignores the stop request
        if self.behaviour == "escape":
            raise Escaped
        await stop.wait()


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> FakeScheduler:
    fake = FakeScheduler(getattr(request, "param", "normal"))

    def build(*_: Any, health: JobHealth | None = None, **__: Any) -> FakeScheduler:
        fake.builds += 1
        if fake.behaviour == "build_fails_once" and fake.builds == 1:
            raise ValueError("a renderer is missing")
        fake.health = health
        return fake

    monkeypatch.setattr(app.worker, "build_scheduler", build)
    monkeypatch.setattr(app.worker, "RESTART_SECONDS", 0.01)
    monkeypatch.setattr(app.worker, "STOP_SECONDS", 0.2)
    return fake


def runner(exits: list[str] | None = None) -> JobRunner:
    settings = worker()
    return JobRunner(
        settings,
        database_uri=settings.mongodb_uri,
        application_name="test-jobs",
        on_unexpected_exit=None if exits is None else lambda: exits.append("exit"),
    )


async def test_one_process_runs_one_scheduler(scheduler: FakeScheduler) -> None:
    jobs = runner()
    jobs.start()
    with pytest.raises(RuntimeError, match="already running"):
        jobs.start()
    await asyncio.sleep(0.05)
    await jobs.stop()
    assert scheduler.runs == 1


@pytest.mark.parametrize("scheduler", ["fail_once"], indirect=True)
async def test_an_unexpected_error_restarts_the_scheduler(scheduler: FakeScheduler) -> None:
    jobs = runner()
    jobs.start()
    await asyncio.sleep(0.2)
    await jobs.stop()
    assert scheduler.runs == 2


@pytest.mark.parametrize("scheduler", ["hang"], indirect=True)
async def test_a_task_that_does_not_stop_in_time_is_cancelled(scheduler: FakeScheduler) -> None:
    jobs = runner()
    jobs.start()
    await asyncio.sleep(0.05)
    await asyncio.wait_for(jobs.stop(), timeout=5)
    assert scheduler.runs == 1


async def test_stopping_jobs_that_never_started_does_nothing() -> None:
    await runner().stop()


@pytest.mark.parametrize("scheduler", ["build_fails_once"], indirect=True)
async def test_an_error_while_building_the_scheduler_restarts_it(scheduler: FakeScheduler) -> None:
    jobs = runner()
    jobs.start()
    await asyncio.sleep(0.2)
    assert jobs.health.healthy()
    await jobs.stop()
    assert scheduler.builds == 2
    assert scheduler.runs == 1


async def test_the_scheduler_beats_the_runners_health(scheduler: FakeScheduler) -> None:
    jobs = runner()
    assert not jobs.health.healthy()  # not started
    jobs.start()
    await asyncio.sleep(0.05)
    assert scheduler.health is jobs.health
    assert jobs.health.healthy()
    await jobs.stop()
    # Stopped on request: not a failure.
    assert jobs.health.failure is None


@pytest.mark.parametrize("scheduler", ["escape"], indirect=True)
async def test_a_runner_that_ends_unexpectedly_is_failed_and_stops_the_process(
    scheduler: FakeScheduler, caplog: pytest.LogCaptureFixture
) -> None:
    exits: list[str] = []
    jobs = runner(exits)
    jobs.start()
    assert jobs._task is not None
    # Wait for the task's end, not a fixed time: its database and HTTP clients are real, and
    # opening and closing them can take longer than a short sleep. `_finished` runs before
    # this wait returns (done callbacks run in the order they were added).
    done, _ = await asyncio.wait([jobs._task], timeout=5)
    assert done
    assert exits == ["exit"]
    assert jobs.health.failure == "Escaped"
    assert not jobs.health.healthy()
    assert [r.levelname for r in caplog.records if r.message == "jobs.runner_stopped"] == ["CRITICAL"]
    await jobs.stop()
    assert exits == ["exit"]


async def test_a_cancelled_runner_is_failed(scheduler: FakeScheduler) -> None:
    exits: list[str] = []
    jobs = runner(exits)
    jobs.start()
    await asyncio.sleep(0.05)
    assert jobs._task is not None
    jobs._task.cancel()
    await asyncio.sleep(0.05)
    assert jobs.health.failure == "cancelled"
    assert exits == ["exit"]


async def test_stopping_the_runner_does_not_stop_the_process(scheduler: FakeScheduler) -> None:
    exits: list[str] = []
    jobs = runner(exits)
    jobs.start()
    await asyncio.sleep(0.05)
    await jobs.stop()
    assert exits == []


def test_health_is_stale_without_a_recent_beat() -> None:
    now = [100.0]
    health = JobHealth(stale_after=timedelta(seconds=60), monotonic=lambda: now[0])
    assert not health.healthy()
    health.beat()
    now[0] += 60
    assert health.healthy()
    now[0] += 1
    assert not health.healthy()
    health.beat()
    assert health.healthy()
    health.fail("returned")
    health.beat()
    assert not health.healthy()
