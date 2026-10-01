"""Database engine, declarative base and unit of work."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy import DateTime, MetaData, Text, Uuid, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.platform.config import sqlalchemy_url

# Constraint names per docs/database-design.md §1. Exclusion constraints (ex_) are named
# explicitly in migrations because SQLAlchemy has no naming token for them.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every module's models.

    Python annotations map to the column types the migrations use: `text` (lengths are
    CHECK constraints, docs/database-design.md §1), `timestamptz` and native `uuid`.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: dict[Any, Any] = {  # noqa: RUF012 (SQLAlchemy reads this class attribute)
        str: Text(),
        datetime: DateTime(timezone=True),
        uuid.UUID: Uuid(),
    }


def create_engine(url: SecretStr, *, application_name: str) -> AsyncEngine:
    return create_async_engine(
        sqlalchemy_url(url),
        pool_pre_ping=True,
        # Never include bound parameters in exception messages; they can hold personal data.
        hide_parameters=True,
        connect_args={"application_name": application_name},
    )


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[AsyncSession]:
        """One transaction: commits when the block succeeds, rolls back when it raises."""
        async with self._sessions() as session, session.begin():
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
