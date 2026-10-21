"""Versioned MongoDB migrations (docs/database-design.md §11).

`app.platform.schema.migrate` applies the declared collections, validators and indexes on every
run, then each version below whose number is not yet recorded in `schema_migrations`. A
version changes data only, never drops a collection or an index, and is idempotent: running it
twice leaves the same result. Add new versions at the end with the next number; never edit or
renumber one that has run anywhere.
"""

from typing import Final

from app.platform.db import Database
from app.platform.schema import Migration


async def _baseline(database: Database) -> None:
    """The first MongoDB release: everything it needs is the declared collections, validators
    and indexes, which the runner has just applied. Recorded so later versions number from 2."""


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "baseline: collections, validators and indexes of the first MongoDB release", _baseline),
)
