"""Erasure requests (owner decision 2026-10-23, docs/database-design.md §9.1).

A request names the employee record by ID only, with the requesting super admin's reason. It
is history: never deleted, statuses only move forward, and a decision is recorded once. The
result holds a category, an outcome, a count and a fixed explanation per kind of record, never
a value from the records. The stored files still to be removed are listed by their random
storage keys, which hold nothing a person typed; the worker empties the list.
"""

import dataclasses
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import Index, Record, allowed, asc, utcnow


class ErasureStatus(StrEnum):
    PENDING = "pending"  # waiting for a second super admin
    REJECTED = "rejected"
    CANCELLED = "cancelled"  # withdrawn by the requester
    # The records are erased; the worker is removing the stored files. Not complete yet.
    FILES_PENDING = "files_pending"
    COMPLETED = "completed"


@dataclasses.dataclass(kw_only=True, eq=False)
class ErasureRequest(Record):
    __collection__ = "employee_erasures"
    __deletable__ = False
    __indexes__ = (
        # One open request (waiting, or erased with files still to remove) per employee.
        Index(
            asc("organization_id", "employee_id"),
            name="uq_employee_erasures_open",
            unique=True,
            partial={"open": True},
        ),
        Index(asc("organization_id", "status", "requested_at"), name="ix_employee_erasures_status"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "status": allowed(ErasureStatus),
        "reason": {"minLength": 1, "maxLength": 500},
        "decision_note": {"minLength": 1, "maxLength": 500},
        "pending_storage_keys": {"maxItems": 10000},
        "files_total": {"minimum": 0},
        "files_removed": {"minimum": 0},
        "file_attempts": {"minimum": 0},
        "last_file_error": {"maxLength": 100},
        "version": {"minimum": 1},
    }
    __expr_rules__ = (
        # Separation of duties: the decider is never the requester.
        {"$or": [{"$eq": ["$decided_by", None]}, {"$ne": ["$decided_by", "$requested_by"]}]},
        {"$eq": [{"$eq": ["$decided_by", None]}, {"$eq": ["$decided_at", None]}]},
        {"$eq": ["$open", {"$in": ["$status", ["pending", "files_pending"]]}]},
        # Complete only once no stored file is left to remove.
        {"$or": [{"$ne": ["$status", "completed"]}, {"$eq": [{"$size": "$pending_storage_keys"}, 0]}]},
        {"$eq": [{"$eq": ["$status", "completed"]}, {"$ne": ["$completed_at", None]}]},
    )
    __mutable__ = frozenset(
        {
            "status",
            "open",
            "decided_by",
            "decided_at",
            "decision_note",
            "result",
            "pending_storage_keys",
            "files_total",
            "files_removed",
            "file_attempts",
            "last_file_error",
            "completed_at",
            "updated_at",
            "version",
        }
    )
    __set_once__ = frozenset({"decided_by", "decided_at", "decision_note", "completed_at"})
    __transitions__: ClassVar[dict[str, dict[str, frozenset[str]]]] = {
        "status": {
            "pending": frozenset({"rejected", "cancelled", "files_pending", "completed"}),
            "files_pending": frozenset({"completed"}),
        }
    }

    employee_id: uuid.UUID
    # Why, typed by the requester; never the person's details (the form says so).
    reason: str
    requested_by: uuid.UUID
    requested_at: datetime = dataclasses.field(default_factory=utcnow)
    status: str = ErasureStatus.PENDING.value
    open: bool = True
    # The super admin who approved and ran it, or rejected it.
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    # Per category: {key, outcome, count, note}.
    result: list[dict[str, Any]] | None = None
    pending_storage_keys: list[str] = dataclasses.field(default_factory=list)
    files_total: int = 0
    files_removed: int = 0
    file_attempts: int = 0
    last_file_error: str | None = None
    completed_at: datetime | None = None
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)
