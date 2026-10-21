"""The audit module's interface: the alert when the audit chain fails verification
(docs/security-architecture.md §8.1, step 5 of the daily verification).

`chain_mismatch_alert` builds the worker's `MismatchHandler`: after the
`audit.chain_mismatch` security event is recorded, it queues an email to every active super
admin and auditor of every organization (the chain covers them all), once per stream, check
scope and day. The email says which stream failed and how many problems were found; the
problems themselves (positions, months, check names) are in the worker log and the security
event, never in email.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

from app.modules.access import public as access
from app.modules.identity import public as identity
from app.modules.notify.public import EmailTemplate, OutboxItem, Renderer, enqueue
from app.platform.audit.integrity import MismatchHandler
from app.platform.audit.verify import Report
from app.platform.authz.roles import RoleKey
from app.platform.db import ID, Database, Session
from app.platform.email import OutgoingEmail
from app.platform.tenancy import Organization, OrganizationStatus

PRODUCT = "Sigvitas HRMS"
SIGN_OFF = f"\n\n{PRODUCT}\nThis is an automatic message. Replies are not read."
STREAM_NAMES = {"audit_log": "audit log", "security_events": "security event log"}

__all__ = ["AuditEmails", "chain_mismatch_alert"]


def chain_mismatch_alert(database: Database) -> MismatchHandler:
    """The alert for every active organization, each in its own transaction."""

    async def alert(report: Report, now: datetime) -> None:
        organizations = database.collection(Organization.__collection__)
        async for item in organizations.find({"status": OrganizationStatus.ACTIVE.value}, projection={ID: 1}):
            async with database.unit_of_work(tenant=item[ID]) as session:
                await _alert_organization(session, report, now)

    return alert


async def _alert_organization(session: Session, report: Report, now: datetime) -> None:
    holders = await access.holder_ids(session, [RoleKey.SUPER_ADMIN.value, RoleKey.AUDITOR.value], now)
    day = now.astimezone(UTC).date().isoformat()
    for user_id in await identity.active_user_ids(session, holders):
        await enqueue(
            session,
            user_id=user_id,
            template=EmailTemplate.AUDIT_CHAIN_MISMATCH,
            idempotency_key=f"audit.chain_mismatch:{report.stream.value}:{report.scope.value}:{day}:{user_id}",
            now=now,
            data={"stream": report.stream.value, "findings": len(report.findings)},
        )


def message(data: Mapping[str, object]) -> tuple[str, str] | None:
    stream = STREAM_NAMES.get(str(data.get("stream")))
    findings = data.get("findings")
    if stream is None or not isinstance(findings, int):
        return None
    problems = f"{findings} problem{'' if findings == 1 else 's'}"
    return (
        "Audit records failed an integrity check",
        f"The scheduled integrity check of the {stream} found {problems}. Audit records may have been "
        "changed, removed or added outside the application.\n\nNothing was changed automatically. "
        "Review the audit.chain_mismatch security event and the worker log, which name each problem, "
        "and compare the exported checkpoints kept outside the database.",
    )


class AuditEmails:
    def renderers(self) -> Mapping[EmailTemplate, Renderer]:
        return {EmailTemplate.AUDIT_CHAIN_MISMATCH: self.chain_mismatch}

    async def chain_mismatch(self, session: Session, item: OutboxItem) -> OutgoingEmail | None:
        address = await identity.active_email(session, item.user_id)
        text = message(item.data)
        if address is None or text is None:
            return None
        subject, paragraph = text
        return OutgoingEmail(
            to=address, subject=f"{subject} ({PRODUCT})", body=f"Hello,\n\n{paragraph}{SIGN_OFF}"
        )
