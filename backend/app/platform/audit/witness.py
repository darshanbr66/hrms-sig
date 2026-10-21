"""The audit witness: signed checkpoint heads kept outside the database (docs/security-architecture.md
§8.1, ADR-030).

Without a copy outside MongoDB, someone with write access to the database could remove the
newest audit records together with their chain links and checkpoints, and verification would
find a shorter but consistent chain. The S3 Object Lock anchors that closed this gap are gone
(no AWS), so the worker sends a witness instead:

- **Daily** (`audit.witness`), one plain-text email to each `AUDIT_WITNESS_EMAILS` mailbox,
  holding one line per stream for the newest checkpoint: its number, hash, key ID and
  signature. The checkpoints form a hash chain, so the head commits to every earlier
  checkpoint and, through them, to every sealed record. A head is witnessed only after its
  stream's checkpoint chain verifies (signatures, numbering, links); a stream whose chain does
  not is left out, and the email says so, so a forged or damaged head is never vouched for.
- **Verification**: `python -m app.cli audit-verify --witness FILE` reads witness lines saved
  from those emails, checks each signature with the public keys, and reports any witnessed
  checkpoint the database no longer holds or holds with another hash.

What it gives and what it does not: the mailboxes are outside the HRMS's database and its
operators' reach only if they belong to someone else (an external auditor, a mailbox at
another provider). It is a witness, not write-once storage: it detects truncation or
rewriting up to the last witnessed head, it does not prevent it, and anything recorded after
the last email is covered only by the next one. Lines hold hashes and signatures, never record
contents.
"""

import base64
import binascii
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.platform.audit import chain
from app.platform.audit.canonical import Stream
from app.platform.audit.chain import StoredCheckpoint
from app.platform.audit.signing import SignatureVerifier
from app.platform.audit.verify import verify_checkpoints
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.email import EmailSender, OutgoingEmail
from app.platform.jobs import Scheduler

logger = logging.getLogger(__name__)

TASK_NAME: Final = "audit.witness"
# After the daily verification (02:30), so a witnessed head has just been verified.
SCHEDULE: Final = "50 2 * * *"
PREFIX: Final = "hrms-audit-witness v1"
_LINE: Final = re.compile(
    r"^hrms-audit-witness v1 stream=(?P<stream>[a-z_]+) checkpoint=(?P<number>\d+) "
    r"hash=(?P<hash>[0-9a-f]{64}) key=(?P<key>\S+) signature=(?P<signature>[A-Za-z0-9+/=]+)$"
)


@dataclass(frozen=True, slots=True)
class WitnessLine:
    stream: Stream
    checkpoint_no: int
    checkpoint_hash: bytes
    key_id: str
    signature: bytes

    def text(self) -> str:
        return (
            f"{PREFIX} stream={self.stream.value} checkpoint={self.checkpoint_no} "
            f"hash={self.checkpoint_hash.hex()} key={self.key_id} "
            f"signature={base64.b64encode(self.signature).decode()}"
        )

    @classmethod
    def of(cls, checkpoint: StoredCheckpoint) -> "WitnessLine":
        return cls(
            stream=checkpoint.fields.stream,
            checkpoint_no=checkpoint.fields.checkpoint_no,
            checkpoint_hash=checkpoint.checkpoint_hash,
            key_id=checkpoint.fields.key_id,
            signature=checkpoint.signature,
        )


def parse_lines(text: Iterable[str]) -> list[WitnessLine]:
    """Every witness line in `text` (other lines, such as the rest of a saved email, are skipped)."""
    found: list[WitnessLine] = []
    for raw in text:
        match = _LINE.match(raw.strip())
        if match is None:
            continue
        try:
            stream = Stream(match["stream"])
            signature = base64.b64decode(match["signature"], validate=True)
        except (ValueError, binascii.Error):
            continue
        found.append(
            WitnessLine(stream, int(match["number"]), bytes.fromhex(match["hash"]), match["key"], signature)
        )
    return found


@dataclass(frozen=True, slots=True)
class Heads:
    lines: list[WitnessLine]
    # Streams with checkpoints whose checkpoint chain did not verify: not witnessed.
    withheld: list[Stream]


async def heads(database: Database, verifier: SignatureVerifier) -> Heads:
    """The newest checkpoint of every stream that has one and whose checkpoint chain verifies."""
    lines: list[WitnessLine] = []
    withheld: list[Stream] = []
    for stream in Stream:
        async with database.unit_of_work() as session:
            stored = await chain.checkpoints(session, stream)
        if not stored:
            continue
        if not (await verify_checkpoints(database, verifier, stream)).ok:
            withheld.append(stream)
            continue
        lines.append(WitnessLine.of(stored[-1]))
    return Heads(lines, withheld)


def witness_email(
    recipient: str, lines: Sequence[WitnessLine], now: datetime, withheld: Sequence[Stream] = ()
) -> OutgoingEmail:
    day = now.astimezone(UTC).date().isoformat()
    notices = [
        f"Not witnessed: the signed checkpoints of the {stream.value} stream did not verify today. "
        "Keep your earlier witness emails and ask for the audit trail to be checked."
        for stream in withheld
    ]
    body = "\n".join(
        [
            f"Sigvitas HRMS audit witness for {day} (UTC).",
            "",
            "Keep this email unchanged. Each line below is the newest signed checkpoint of one audit",
            "stream. To check the audit trail, save these lines to a file and run:",
            "python -m app.cli audit-verify --witness FILE",
            "",
            *(line.text() for line in lines),
            *(["", *notices] if notices else []),
            "",
            "This is an automatic message. Replies are not read.",
        ]
    )
    return OutgoingEmail(to=recipient, subject=f"HRMS audit witness {day}", body=body)


async def send_witness(
    database: Database,
    sender: EmailSender,
    recipients: Sequence[str],
    now: datetime,
    *,
    verifier: SignatureVerifier,
) -> int:
    """Send today's witness to every recipient; returns the number of lines sent. A stream
    whose checkpoint chain does not verify is named in the email instead of witnessed."""
    found = await heads(database, verifier)
    for stream in found.withheld:
        logger.error("audit.witness_withheld", extra={"stream": stream.value})
    if not found.lines and not found.withheld:
        return 0
    for recipient in recipients:
        await sender.send(witness_email(recipient, found.lines, now, found.withheld))
    logger.info(
        "audit.witness_sent",
        extra={"lines": len(found.lines), "withheld": len(found.withheld), "recipients": len(recipients)},
    )
    return len(found.lines)


@dataclass(frozen=True, slots=True)
class WitnessFinding:
    check: str
    stream: str
    detail: str


async def verify_witness(
    database: Database, verifier: SignatureVerifier, lines: Sequence[WitnessLine]
) -> list[WitnessFinding]:
    """Compare witnessed checkpoint heads with the database. Read-only."""
    findings: list[WitnessFinding] = []
    async with database.unit_of_work() as session:
        stored = {stream: await chain.checkpoints(session, stream) for stream in Stream}
    for line in lines:
        where = f"checkpoint {line.checkpoint_no}"
        if not verifier.verify(line.key_id, line.checkpoint_hash, line.signature):
            findings.append(WitnessFinding("witness_signature_invalid", line.stream.value, where))
            continue
        match = next(
            (item for item in stored[line.stream] if item.fields.checkpoint_no == line.checkpoint_no), None
        )
        if match is None:
            findings.append(WitnessFinding("witnessed_checkpoint_missing", line.stream.value, where))
        elif match.checkpoint_hash != line.checkpoint_hash:
            findings.append(WitnessFinding("witnessed_checkpoint_changed", line.stream.value, where))
    return findings


def register_audit_witness(
    scheduler: Scheduler,
    database: Database,
    *,
    sender: EmailSender,
    recipients: Sequence[str],
    verifier: SignatureVerifier,
    clock: Clock,
) -> None:
    """The daily witness; without recipients (allowed outside production) a daily warning says
    the trail is not witnessed."""

    @scheduler.periodic(TASK_NAME, SCHEDULE, attempts=3)
    async def witness() -> None:
        if not recipients:
            logger.warning("audit.witness_not_configured")
            return
        await send_witness(database, sender, recipients, clock.now(), verifier=verifier)
