"""What the permanent erasure of one person's data did to each kind of record (owner decision
2026-10-23, docs/database-design.md §9.1).

Every module that holds data about a person reports its part as `Category` rows: the kind of
record, what happened to it (deleted, anonymized, retained, or waiting for the worker), how many
records, and why. The rows carry counts and fixed explanations only, never a value from the
records, so the report can be stored and shown without copying the personal data it describes.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class Outcome(StrEnum):
    DELETED = "deleted"
    ANONYMIZED = "anonymized"
    # Kept as it is: history that must not be changed (CLAUDE.md rules 5 and 19), or data with
    # no personal content; the note says which and why.
    RETAINED = "retained"
    # Removal is queued for the worker (files): not done until the request says it is complete.
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class Category:
    key: str
    outcome: Outcome
    count: int
    note: str


@dataclass(slots=True)
class Erased:
    """One module's part: its categories, the records whose notifications go too, and the
    stored files the worker must remove."""

    categories: list[Category] = field(default_factory=list)
    subject_ids: list[uuid.UUID] = field(default_factory=list)
    storage_keys: list[str] = field(default_factory=list)

    def add(self, key: str, outcome: Outcome, count: int, note: str) -> None:
        self.categories.append(Category(key, outcome, count, note))
