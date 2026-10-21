"""Liveness and readiness (docs/architecture.md §11). Neither reveals versions or configuration.

Readiness covers the database: MongoDB holds everything (data, sessions, rate-limit counters,
jobs, files), so without it the API can do nothing useful and is taken out of rotation. When the
process also runs the scheduled jobs (`RUN_JOBS_IN_API`), readiness covers them too: jobs that
have stopped or stopped beating (`JobHealth`) take the API out of rotation, so the outbox,
audit sealing and the witness never stop silently.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from pymongo.errors import PyMongoError

from app.platform.authz.route import public_route
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.jobs import JobHealth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"])


class HealthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


@router.get("/live", response_model=HealthStatus, dependencies=[public_route()])
async def live() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/ready", response_model=HealthStatus, dependencies=[public_route()])
async def ready(request: Request) -> HealthStatus:
    database: Database = request.app.state.database
    try:
        await database.ping()
    except (PyMongoError, OSError) as exc:
        logger.warning("health.database_unavailable", extra={"error_type": type(exc).__name__})
        raise ProblemError(ProblemType.TEMPORARILY_UNAVAILABLE, headers={"Retry-After": "5"}) from exc
    jobs: JobHealth | None = getattr(request.app.state, "job_health", None)
    if jobs is not None and not jobs.healthy():
        logger.warning("health.jobs_unhealthy", extra={"reason": jobs.failure or "no_heartbeat"})
        raise ProblemError(ProblemType.TEMPORARILY_UNAVAILABLE, headers={"Retry-After": "5"})
    return HealthStatus(status="ok")
