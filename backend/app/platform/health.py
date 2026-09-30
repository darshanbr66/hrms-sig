"""Liveness and readiness (docs/architecture.md §11). Neither reveals versions or configuration.

Readiness covers the database only. Redis is deliberately excluded: rate limiting degrades
per scope when it is down, so it must not take the API out of rotation. Object storage is
added to readiness when the API starts using it.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"])


class HealthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


@router.get("/live", response_model=HealthStatus)
async def live() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/ready", response_model=HealthStatus)
async def ready(request: Request) -> HealthStatus:
    database: Database = request.app.state.database
    try:
        await database.ping()
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("health.database_unavailable", extra={"error_type": type(exc).__name__})
        raise ProblemError(ProblemType.TEMPORARILY_UNAVAILABLE, headers={"Retry-After": "5"}) from exc
    return HealthStatus(status="ok")
