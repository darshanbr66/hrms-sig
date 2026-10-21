"""The exact byte encodings of the audit integrity chain (docs/security-architecture.md §8.1).

Everything here is a pure function, shared by the sealer, the checkpoint writer, verification
and retention, so the value that is sealed and the value that is checked are computed by the
same code.

- **Row digest** (`digest_version` 1): SHA-256 of the two-byte version followed by the
  RFC 8785 canonical JSON of the record: an object of every field of the stream (`COLUMNS`,
  fixed per version) to its canonical form (`canonical_value`): UUIDs as lower-case text,
  times as UTC ISO 8601 with milliseconds and `Z`, bytes as lower-case hex, objects as JSON,
  absent values as null. A new field needs a new digest version.
- **Genesis** of a partition: SHA-256 of `sv-audit-genesis`, the stream and the month key,
  NUL-separated. The chain restarts in every partition.
- **Link**: SHA-256 of the previous link (or the genesis), the row digest and the position
  as an 8-byte big-endian integer: fixed sizes, so the input is unambiguous.
- **Checkpoint hash**: SHA-256 of `sv-audit-checkpoint`, NUL, and the canonical JSON of the
  checkpoint's fields (`checkpoint_document`); hashes are lower-case hex, the time is UTC
  ISO 8601 with microseconds. The Ed25519 signature is over the 32-byte checkpoint hash.
- **Export**: the checkpoint document plus its hash and signature, as canonical JSON
  (`export_body`). `python -m app.cli audit-export-checkpoints` writes them out so an operator
  can keep copies outside the database (docs/security-architecture.md §8.1: there is no
  write-once storage in this deployment, so these copies are what detects a rewrite by someone
  with database administrator access).
"""

import base64
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import rfc8785

DIGEST_VERSION: Final = 1
HASH_BYTES: Final = 32
ZERO_HASH: Final = bytes(HASH_BYTES)
# A month can no longer receive records one hour after it ends: longer than any transaction
# lives (MongoDB aborts transactions after 60 seconds).
FINAL_MARGIN: Final = timedelta(hours=1)


class Stream(StrEnum):
    AUDIT_LOG = "audit_log"
    SECURITY_EVENTS = "security_events"


class CheckpointKind(StrEnum):
    DAILY = "daily"
    PARTITION_FINAL = "partition_final"
    RETENTION_TOMBSTONE = "retention_tombstone"


# Every column of each stream, in table order, as sealed by digest version 1.
COLUMNS: Final[Mapping[Stream, tuple[str, ...]]] = {
    Stream.AUDIT_LOG: (
        "id",
        "organization_id",
        "recorded_at",
        "occurred_at",
        "actor_type",
        "actor_user_id",
        "session_id",
        "grant_request_id",
        "request_id",
        "ip",
        "user_agent",
        "action",
        "permission_used",
        "target_type",
        "target_id",
        "subject_employee_id",
        "outcome",
        "changes",
        "reason",
    ),
    Stream.SECURITY_EVENTS: (
        "id",
        "organization_id",
        "recorded_at",
        "occurred_at",
        "event_type",
        "severity",
        "user_id",
        "session_id",
        "request_id",
        "email_attempted_hash",
        "ip",
        "user_agent",
        "details",
    ),
}


# --- months ---------------------------------------------------------------------------------


def month_key(moment: datetime) -> str:
    """`YYYY-MM` of the UTC month containing `moment`."""
    return moment.astimezone(UTC).strftime("%Y-%m")


def month_start(key: str) -> datetime:
    return datetime.strptime(f"{key}-01", "%Y-%m-%d").replace(tzinfo=UTC)


def add_months(moment: datetime, months: int) -> datetime:
    """The first instant of the month `months` after the month starting at `moment`."""
    index = moment.year * 12 + moment.month - 1 + months
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def month_end(key: str) -> datetime:
    return add_months(month_start(key), 1)


# --- row chain ------------------------------------------------------------------------------


def _sha256(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def canonical_value(value: Any) -> Any:  # noqa: PLR0911 (one form per stored type)
    """A stored value in the form the row digest covers."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    return str(value)


def row_values(stream: Stream, stored: Mapping[str, Any]) -> dict[str, Any]:
    """The digest input of a stored record: every field of the stream, canonical."""
    return {name: canonical_value(stored.get("_id" if name == "id" else name)) for name in COLUMNS[stream]}


def row_digest(stream: Stream, values: Mapping[str, Any], version: int = DIGEST_VERSION) -> bytes:
    if version != DIGEST_VERSION:
        raise ValueError(f"unknown digest version {version}")
    columns = COLUMNS[stream]
    if set(values) != set(columns):
        raise ValueError("a row digest covers exactly the stream's columns")
    return _sha256(version.to_bytes(2, "big"), rfc8785.dumps({name: values[name] for name in columns}))


def genesis(stream: Stream, key: str) -> bytes:
    return _sha256(b"sv-audit-genesis\x00", stream.value.encode(), b"\x00", key.encode())


def link_hash(previous: bytes, digest: bytes, position: int) -> bytes:
    if len(previous) != HASH_BYTES or len(digest) != HASH_BYTES or position < 1:
        raise ValueError("a link needs two 32-byte hashes and a position from 1")
    return _sha256(previous, digest, position.to_bytes(8, "big"))


# --- checkpoints ----------------------------------------------------------------------------


def timestamp_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class CheckpointFields:
    """What a checkpoint states; its hash and signature are computed from exactly this."""

    stream: Stream
    checkpoint_no: int
    kind: CheckpointKind
    partition_key: str
    last_position: int
    row_count: int
    last_link_hash: bytes
    prev_checkpoint_hash: bytes
    key_id: str
    created_at: datetime
    archive_sha256: bytes | None = None
    retention_policy_version: str | None = None


def checkpoint_document(fields: CheckpointFields) -> dict[str, str | int | None]:
    return {
        "version": 1,
        "stream": fields.stream.value,
        "checkpoint_no": fields.checkpoint_no,
        "kind": fields.kind.value,
        "partition_key": fields.partition_key,
        "last_position": fields.last_position,
        "row_count": fields.row_count,
        "last_link_hash": fields.last_link_hash.hex(),
        "prev_checkpoint_hash": fields.prev_checkpoint_hash.hex(),
        "key_id": fields.key_id,
        "created_at": timestamp_text(fields.created_at),
        "archive_sha256": fields.archive_sha256.hex() if fields.archive_sha256 is not None else None,
        "retention_policy_version": fields.retention_policy_version,
    }


def checkpoint_hash(fields: CheckpointFields) -> bytes:
    return _sha256(b"sv-audit-checkpoint\x00", rfc8785.dumps(checkpoint_document(fields)))


def export_body(fields: CheckpointFields, signature: bytes) -> bytes:
    """A checkpoint as kept outside the database: what it states, its hash and its signature."""
    document = checkpoint_document(fields) | {
        "checkpoint_hash": checkpoint_hash(fields).hex(),
        "signature": base64.b64encode(signature).decode(),
    }
    return rfc8785.dumps(document)
