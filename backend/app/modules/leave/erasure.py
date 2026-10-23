"""The leave module's part of permanently erasing one person's data (owner decision 2026-10-23,
docs/database-design.md §9.1).

The reasons typed on leave requests can hold personal details, so they are overwritten with the
erasure marker. The requests themselves (type, dates, days, status), the decisions and the
ledger are leave history and balances that payroll and audit depend on: they stay, under the
erased record's ID. Decisions and ledger entries are append-only (rule 5), so a note typed on a
decision or a reason on an adjustment stays as it is.
"""

import uuid

from app.modules.leave.models import LeaveDecision, LeaveLedgerEntry, LeaveRequest
from app.platform.db import ERASED, Session
from app.platform.erasure import Erased, Outcome


async def erase_employee(session: Session, employee_id: uuid.UUID) -> Erased:
    result = Erased()
    requests = await session.find(LeaveRequest, {"employee_id": employee_id})
    reasoned = [request for request in requests if request.reason is not None and request.reason != ERASED]
    for request in reasoned:
        request.reason = ERASED
    await session.flush()
    result.subject_ids.extend(request.id for request in requests)
    result.add("leave.request_reasons", Outcome.ANONYMIZED, len(reasoned), "Reasons typed on leave requests.")
    result.add(
        "leave.requests",
        Outcome.RETAINED,
        len(requests),
        "Leave type, dates, days and status: leave history that balances and payroll depend on.",
    )
    ids = [request.id for request in requests]
    decisions = await session.count(LeaveDecision, {"leave_request_id": {"$in": ids}}) if ids else 0
    result.add(
        "leave.decisions",
        Outcome.RETAINED,
        decisions,
        "Who decided and when, with any note: append-only history (CLAUDE.md rule 5).",
    )
    ledger = await session.count(LeaveLedgerEntry, {"employee_id": employee_id})
    result.add(
        "leave.ledger",
        Outcome.RETAINED,
        ledger,
        "Accruals, deductions and adjustments with their reasons: the append-only balance ledger (rule 5).",
    )
    return result
