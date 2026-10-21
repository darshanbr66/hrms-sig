"""Witness lines: what the daily email carries and what `audit-verify --witness` reads back."""

from datetime import UTC, datetime

from app.platform.audit.canonical import Stream
from app.platform.audit.witness import WitnessLine, parse_lines, witness_email

LINE = WitnessLine(
    stream=Stream.AUDIT_LOG,
    checkpoint_no=42,
    checkpoint_hash=bytes(range(32)),
    key_id="key-2026",
    signature=b"s" * 64,
)


def test_a_line_reads_back_as_written() -> None:
    assert parse_lines([LINE.text()]) == [LINE]


def test_the_email_holds_every_line_and_reads_back_from_its_body() -> None:
    other = WitnessLine(Stream.SECURITY_EVENTS, 7, b"\xff" * 32, "key-2026", b"t" * 64)
    email = witness_email("auditor@example.org", [LINE, other], datetime(2026, 10, 20, 3, tzinfo=UTC))
    assert email.to == "auditor@example.org"
    assert "2026-10-20" in email.subject
    # A saved email, quoted or with mail headers around it, still yields exactly the lines.
    saved = "From: hrms@example.com\n> " + email.body.replace("\n", "\n> ")
    assert parse_lines(saved.replace("> ", "").splitlines()) == [LINE, other]


def test_malformed_lines_are_skipped() -> None:
    good = LINE.text()
    bad = [
        good.replace("stream=audit_log", "stream=unknown_stream"),
        good.replace("checkpoint=42", "checkpoint=x"),
        good.replace(bytes(range(32)).hex(), "00"),
        good.replace("signature=", "signature=***"),
        "hrms-audit-witness v2 " + good.split(" ", 2)[2],
    ]
    assert parse_lines(bad) == []
