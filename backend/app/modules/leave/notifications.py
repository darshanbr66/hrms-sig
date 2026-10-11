"""Emails about leave requests (product-requirements NTF-2), written in the caller's
transaction through the notify outbox.

- A request waiting for a decision (submitted, or a cancellation asked for) reaches the
  employee's reporting manager on the day it is sent, if they have an active account. HR sees
  every waiting request in the approvals inbox; nobody else is emailed.
- A decision, and leave recorded at once for a type that needs no approval, reaches the
  employee.

The rows carry the event, the leave type's name, the dates and the day count: never the reason
or a decision note, which may hold personal details, and never an address.
"""

import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import public as identity
from app.modules.leave.models import LeaveRequest
from app.modules.notify.public import EmailTemplate, enqueue
from app.modules.people import public as people


def _data(request: LeaveRequest, leave_type: str, event: str) -> dict[str, str | int | bool | None]:
    return {
        "event": event,
        "leave_type": leave_type[:100],
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "days": str(request.days_requested),
        "request_id": str(request.id),
    }


async def _send(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    request: LeaveRequest,
    leave_type: str,
    event: str,
    now: datetime,
) -> None:
    if user_id is None:
        return
    await enqueue(
        session,
        user_id=user_id,
        template=EmailTemplate.LEAVE_REQUEST,
        idempotency_key=f"leave_request:{request.id}:{request.version}:{event}:{user_id}",
        now=now,
        data=_data(request, leave_type, event),
    )


async def to_manager(
    session: AsyncSession, request: LeaveRequest, leave_type: str, event: str, *, now: datetime, today: date
) -> None:
    facts = await people.job_facts(session, request.employee_id, today)
    if facts is None or facts.manager_employee_id is None:
        return
    await _send(
        session,
        user_id=await identity.account_for_employee(session, facts.manager_employee_id),
        request=request,
        leave_type=leave_type,
        event=event,
        now=now,
    )


async def to_employee(
    session: AsyncSession, request: LeaveRequest, leave_type: str, event: str, now: datetime
) -> None:
    await _send(
        session,
        user_id=await identity.account_for_employee(session, request.employee_id),
        request=request,
        leave_type=leave_type,
        event=event,
        now=now,
    )


async def recorded(session: AsyncSession, request: LeaveRequest, leave_type: str, now: datetime) -> None:
    await to_employee(session, request, leave_type, "recorded", now)
