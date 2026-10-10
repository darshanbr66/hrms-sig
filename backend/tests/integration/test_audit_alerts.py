"""The audit chain mismatch alert (docs/security-architecture.md §8.1, daily verification step 5).

A failed check emails every active super admin and auditor once per stream, scope and day,
and nobody else. The email names the stream and the number of problems, never the problems.
"""

from app.modules.audit.public import alert_chain_mismatch
from app.modules.identity.models import UserStatus
from app.platform.audit.canonical import Stream
from app.platform.audit.verify import Finding, Report, Scope
from app.platform.authz.roles import RoleKey
from tests.api_support import Api


async def test_super_admins_and_auditors_are_alerted_once(isolated_api: Api) -> None:
    api = isolated_api
    super_admin = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,))
    auditor = await api.create_account(roles=(RoleKey.AUDITOR.value,))
    hr = await api.create_account(roles=(RoleKey.HR.value,))
    disabled = await api.create_account(roles=(RoleKey.SUPER_ADMIN.value,), status=UserStatus.DISABLED)
    report = Report(
        Stream.AUDIT_LOG,
        Scope.RECENT,
        findings=[
            Finding("row_modified", "2026-10", "position 2"),
            Finding("link_broken", "2026-10", "position 3"),
        ],
    )
    for _ in range(2):
        async with api.worker_database.unit_of_work() as session:
            await alert_chain_mismatch(session, report, api.clock.now())

    sent = await api.deliver()
    assert sorted(message.to for message in sent) == sorted([super_admin.email, auditor.email])
    assert not api.mail_to(hr.email)
    assert not api.mail_to(disabled.email)
    [message] = api.mail_to(auditor.email)
    assert message.subject == "Audit records failed an integrity check (Sigvitas HRMS)"
    assert "The scheduled integrity check of the audit log found 2 problems." in message.body
    for detail in ("position", "row_modified", "2026-10"):
        assert detail not in message.body
