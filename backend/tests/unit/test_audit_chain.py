"""The audit chain's byte encodings and checkpoint signatures (docs/security-architecture.md §8.1).

Each hash is recomputed here from the documented formula, independently of the code, so a
change to an encoding (which would make every existing chain fail verification) cannot pass
unnoticed.
"""

import base64
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import rfc8785

from app.platform.audit import canonical
from app.platform.audit.canonical import CheckpointFields, CheckpointKind, Stream
from app.platform.audit.signing import CheckpointSigner, SignatureVerifier
from app.platform.audit.tables import AuditLogRecord, SecurityEventRecord
from tests.api_support import audit_signer, audit_verifier

ROW = dict.fromkeys(canonical.COLUMNS[Stream.SECURITY_EVENTS]) | {
    "id": "0199d0a4-0000-7000-8000-000000000001",
    "recorded_at": "2026-10-10T08:00:00.123Z",
    "occurred_at": "2026-10-10T08:00:00.120Z",
    "event_type": "login.failed",
    "severity": "warning",
    "details": {"reason": "password"},
}


def fields(**overrides: object) -> CheckpointFields:
    values: dict[str, object] = {
        "stream": Stream.AUDIT_LOG,
        "checkpoint_no": 7,
        "kind": CheckpointKind.DAILY,
        "partition_key": "2026-10",
        "last_position": 42,
        "row_count": 42,
        "last_link_hash": bytes(range(32)),
        "prev_checkpoint_hash": bytes(32),
        "key_id": "test-key",
        "created_at": datetime(2026, 10, 10, 1, 7, 0, 5, tzinfo=UTC),
    }
    return CheckpointFields(**(values | overrides))  # type: ignore[arg-type]


def test_columns_are_exactly_each_streams_fields() -> None:
    # A new field changes every digest: it needs a new digest version, not a silent change. The
    # month is derived from `recorded_at`, so it is not sealed separately.
    for stream, model in ((Stream.AUDIT_LOG, AuditLogRecord), (Stream.SECURITY_EVENTS, SecurityEventRecord)):
        assert set(canonical.COLUMNS[stream]) == set(model.field_types()) - {"month"}


def test_stored_values_have_one_canonical_form() -> None:
    record = {
        "_id": uuid.UUID("0199d0a4-0000-7000-8000-000000000001"),
        "organization_id": None,
        "recorded_at": datetime(2026, 10, 10, 8, 0, 0, 123000, tzinfo=UTC),
        "email_attempted_hash": bytes(range(4)),
        "details": {"attempts": 3, "nested": {"at": datetime(2026, 1, 1, tzinfo=UTC)}},
    }
    values = canonical.row_values(Stream.SECURITY_EVENTS, record)
    assert values["id"] == "0199d0a4-0000-7000-8000-000000000001"
    assert values["recorded_at"] == "2026-10-10T08:00:00.123Z"
    assert values["email_attempted_hash"] == "00010203"
    assert values["details"] == {"attempts": 3, "nested": {"at": "2026-01-01T00:00:00.000Z"}}
    assert values["event_type"] is None
    assert set(values) == set(canonical.COLUMNS[Stream.SECURITY_EVENTS])


def test_row_digest_is_version_then_canonical_json() -> None:
    expected = hashlib.sha256(b"\x00\x01" + rfc8785.dumps(ROW)).digest()
    assert canonical.row_digest(Stream.SECURITY_EVENTS, ROW) == expected
    # Key order does not matter; every value does.
    assert canonical.row_digest(Stream.SECURITY_EVENTS, dict(reversed(ROW.items()))) == expected
    assert canonical.row_digest(Stream.SECURITY_EVENTS, ROW | {"severity": "high"}) != expected
    assert canonical.row_digest(Stream.SECURITY_EVENTS, ROW | {"user_agent": ""}) != expected


def test_row_digest_refuses_other_columns_and_versions() -> None:
    with pytest.raises(ValueError, match="exactly"):
        canonical.row_digest(Stream.SECURITY_EVENTS, ROW | {"extra": "x"})
    with pytest.raises(ValueError, match="exactly"):
        canonical.row_digest(Stream.AUDIT_LOG, ROW)
    with pytest.raises(ValueError, match="digest version"):
        canonical.row_digest(Stream.SECURITY_EVENTS, ROW, version=2)


def test_genesis_and_links() -> None:
    genesis = canonical.genesis(Stream.AUDIT_LOG, "2026-10")
    assert genesis == hashlib.sha256(b"sv-audit-genesis\x00audit_log\x002026-10").digest()
    assert genesis != canonical.genesis(Stream.SECURITY_EVENTS, "2026-10")
    assert genesis != canonical.genesis(Stream.AUDIT_LOG, "2026-11")
    digest = hashlib.sha256(b"row").digest()
    link = canonical.link_hash(genesis, digest, 1)
    assert link == hashlib.sha256(genesis + digest + (1).to_bytes(8, "big")).digest()
    assert canonical.link_hash(genesis, digest, 2) != link
    with pytest.raises(ValueError, match="position"):
        canonical.link_hash(genesis, digest, 0)
    with pytest.raises(ValueError, match="32-byte"):
        canonical.link_hash(b"short", digest, 1)


def test_checkpoint_hash_covers_every_field() -> None:
    base = fields()
    document = canonical.checkpoint_document(base)
    assert document["created_at"] == "2026-10-10T01:07:00.000005Z"
    assert document["last_link_hash"] == bytes(range(32)).hex()
    expected = hashlib.sha256(b"sv-audit-checkpoint\x00" + rfc8785.dumps(document)).digest()
    assert canonical.checkpoint_hash(base) == expected
    changes: list[dict[str, object]] = [
        {"stream": Stream.SECURITY_EVENTS},
        {"checkpoint_no": 8},
        {"kind": CheckpointKind.PARTITION_FINAL},
        {"partition_key": "2026-09"},
        {"last_position": 41, "row_count": 41},
        {"last_link_hash": bytes(32)},
        {"prev_checkpoint_hash": bytes(range(32))},
        {"key_id": "other-key"},
        {"created_at": datetime(2026, 10, 10, 1, 7, 0, 6, tzinfo=UTC)},
        {"retention_policy_version": "retain_months=24"},
        {"archive_sha256": bytes(32)},
    ]
    hashes = {canonical.checkpoint_hash(replace(base, **change)) for change in changes}  # type: ignore[arg-type]
    assert expected not in hashes
    assert len(hashes) == len(changes)


def test_export_body_is_the_checkpoint_with_hash_and_signature() -> None:
    signer = audit_signer()
    base = fields()
    digest = canonical.checkpoint_hash(base)
    signature = signer.sign(digest)
    body = json.loads(canonical.export_body(base, signature))
    assert body["checkpoint_hash"] == digest.hex()
    assert base64.b64decode(body["signature"]) == signature
    assert {key: body[key] for key in canonical.checkpoint_document(base)} == canonical.checkpoint_document(
        base
    )


def test_signatures_verify_only_with_the_named_key() -> None:
    signer, other = audit_signer(), audit_signer("other-key", b"o" * 32)
    digest = canonical.checkpoint_hash(fields())
    signature = signer.sign(digest)
    verifier = audit_verifier(signer, other)
    assert verifier.verify("test-key", digest, signature)
    assert not verifier.verify("other-key", digest, signature)
    assert not verifier.verify("unknown", digest, signature)
    assert not verifier.verify("test-key", canonical.checkpoint_hash(fields(checkpoint_no=8)), signature)
    assert not verifier.verify("test-key", digest, signature[:-1] + bytes([signature[-1] ^ 1]))
    assert not verifier.verify("test-key", digest, signature[:10])
    # Old keys stay listed after a rotation, so old checkpoints still verify.
    assert audit_verifier(other, signer).matches(signer)
    assert not audit_verifier(other).matches(signer)


def test_signing_keys_are_raw_32_byte_keys() -> None:
    with pytest.raises(ValueError, match="base64"):
        CheckpointSigner.from_base64("key", "not base64!")
    with pytest.raises(ValueError, match="32-byte"):
        CheckpointSigner.from_base64("key", base64.b64encode(b"x" * 31).decode())
    with pytest.raises(ValueError, match="key ID"):
        CheckpointSigner.from_base64("", base64.b64encode(b"x" * 32).decode())
    with pytest.raises(ValueError, match="at least one"):
        SignatureVerifier({})


def test_month_helpers() -> None:
    assert canonical.month_key(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == "2026-12"
    assert canonical.month_end("2026-12") == datetime(2027, 1, 1, tzinfo=UTC)
    assert canonical.add_months(datetime(2026, 11, 1, tzinfo=UTC), 14) == datetime(2028, 1, 1, tzinfo=UTC)
