"""The exact byte encodings of the audit integrity chain (docs/security-architecture.md §8.1).

Everything here is a pure function, shared by the sealer, the checkpoint writer, verification
and retention, so the value that is sealed and the value that is checked are computed by the
same code.

- **Row digest** (`digest_version` 1): SHA-256 of the two-byte version followed by the
  RFC 8785 canonical JSON of the row: an object of every column of the stream (`COLUMNS`,
  fixed per version) to its PostgreSQL text form, or null. Text forms are read with
  `TimeZone` UTC, `DateStyle` ISO and hex `bytea` output (`ROW_TEXT_SETTINGS`), so they do
  not depend on the driver or the session. A new column needs a new digest version.
- **Genesis** of a partition: SHA-256 of `sv-audit-genesis`, the stream and the month key,
  NUL-separated. The chain restarts in every partition.
- **Link**: SHA-256 of the previous link (or the genesis), the row digest and the position
  as an 8-byte big-endian integer: fixed sizes, so the input is unambiguous.
- **Checkpoint hash**: SHA-256 of `sv-audit-checkpoint`, NUL, and the canonical JSON of the
  checkpoint's fields (`checkpoint_document`); hashes are lower-case hex, the time is UTC
  ISO 8601 with microseconds. The Ed25519 signature is over the 32-byte checkpoint hash.
- **Anchor**: the checkpoint document plus its hash and signature, as canonical JSON, stored
  under `<stream>/<checkpoint_no, 12 digits>.json` in the write-once bucket.
"""

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

import rfc8785

DIGEST_VERSION: Final = 1
HASH_BYTES: Final = 32
ZERO_HASH: Final = bytes(HASH_BYTES)
# A month partition can no longer receive rows one hour after the month ends: longer than any
# runtime role's transaction_timeout (API 30 s, worker and retention 10 min).
FINAL_MARGIN: Final = timedelta(hours=1)
# Session settings under which rows are read as text for their digest.
ROW_TEXT_SETTINGS: Final = {"TimeZone": "UTC", "DateStyle": "ISO, YMD", "bytea_output": "hex"}


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


def partition_name(stream: Stream, key: str) -> str:
    return f"{stream.value}_p{key.replace('-', '_')}"


def key_of_partition(stream: Stream, name: str) -> str | None:
    """The month of a partition name made by `audit.ensure_partitions`, or None."""
    prefix = f"{stream.value}_p"
    suffix = name.removeprefix(prefix)
    if suffix == name or len(suffix) != 7 or suffix[4] != "_":
        return None
    year, month = suffix[:4], suffix[5:]
    if not (year.isdigit() and month.isdigit() and 1 <= int(month) <= 12):
        return None
    return f"{year}-{month}"


# --- row chain ------------------------------------------------------------------------------


def _sha256(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def row_digest(stream: Stream, values: Mapping[str, str | None], version: int = DIGEST_VERSION) -> bytes:
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

    @property
    def anchor_object_key(self) -> str:
        return anchor_object_key(self.stream, self.checkpoint_no)


def anchor_object_key(stream: Stream, checkpoint_no: int) -> str:
    return f"{stream.value}/{checkpoint_no:012d}.json"


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


def anchor_body(fields: CheckpointFields, signature: bytes) -> bytes:
    """The write-once copy of a checkpoint: what it states, its hash and its signature."""
    document = checkpoint_document(fields) | {
        "checkpoint_hash": checkpoint_hash(fields).hex(),
        "signature": base64.b64encode(signature).decode(),
    }
    return rfc8785.dumps(document)
