"""Versioned MongoDB migrations (docs/database-design.md §11).

`app.platform.schema.migrate` applies the declared collections, validators and indexes on every
run, then each version below whose number is not yet recorded in `schema_migrations`. A
version changes data only, never drops a collection or an index, and is idempotent: running it
twice leaves the same result. A version marked `before_declarations` runs before the
declarations, to give existing documents a field just added to their model (the validators
require every declared field). Add new versions at the end with the next number; never edit or
renumber one that has run anywhere.
"""

from typing import Final

from app.platform.db import Database
from app.platform.schema import Migration


async def _baseline(database: Database) -> None:
    """The first MongoDB release: everything it needs is the declared collections, validators
    and indexes, which the runner has just applied. Recorded so later versions number from 2."""


async def _erased_at(database: Database) -> None:
    """`erased_at` was added to accounts, employee records and document versions (permanent
    erasure, 2026-10-23). The validators require every declared field, so each stored document
    gets it as null (not erased) before they are applied. Only documents without the field are
    touched, so running it again changes nothing. The old validator forbids the field, so this
    one write bypasses it; the new validator is applied and checked right after."""
    for collection in ("users", "employees", "document_versions"):
        await database.collection(collection).update_many(
            {"erased_at": {"$exists": False}}, {"$set": {"erased_at": None}}, bypass_document_validation=True
        )


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "baseline: collections, validators and indexes of the first MongoDB release", _baseline),
    Migration(
        2,
        "erased_at on accounts, employee records and document versions",
        _erased_at,
        before_declarations=True,
    ),
)
