"""Emails about leave requests (product-requirements NTF-2), written in the caller's
transaction through the notify outbox.

- A request waiting for a decision (submitted, or a cancellation asked for) reaches the
  employee's reporting manager on the day it is sent, if they have an active account that is
  not an administrator account (whose team access is withheld). HR sees every waiting request
  in the approvals inbox; nobody else is emailed.
- A decision, and leave recorded at once for a type that needs no approval, reaches the
  employee.

The rows carry the event, the leave type's name, the dates and the day count: never the reason
or a decision note, which may hold personal details, and never an address.

Each event also creates an in-app notification with the same text (NTF-1); a recipient who
muted the type in the app or by email gets neither on that channel (NTF-3).
"""

import uuid
from datetime import date, datetime

from app.modules.access import public as access
from app.modules.identity import public as identity
from app.modules.leave.emails import message
from app.modules.leave.models import LeaveRequest
from app.modules.notify.public import EmailTemplate, NotificationType, email_wanted, enqueue, notify
from app.modules.people import public as people
from app.platform.db import Session

_FOR_DECIDER = frozenset({"submitted", "cancellation_requested"})


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
    session: Session,
    *,
    user_id: uuid.UUID | None,
    request: LeaveRequest,
    leave_type: str,
    event: str,
    now: datetime,
) -> None:
    if user_id is None:
        return
    data = _data(request, leave_type, event)
    key = f"leave_request:{request.id}:{request.version}:{event}:{user_id}"
    kind = (
        NotificationType.LEAVE_APPROVAL_REQUESTED
        if event in _FOR_DECIDER
        else NotificationType.LEAVE_REQUEST_UPDATED
    )
    text = message(data)
    if text is not None:
        title, body, link = text
        if event in _FOR_DECIDER:
            name = (await people.employee_names(session, {request.employee_id})).get(request.employee_id)
            body = body.replace("A member of your team", name, 1) if name else body
        await notify(
            session,
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            link=link if event in _FOR_DECIDER else f"/leave/requests/{request.id}",
            idempotency_key=key,
            subject=("leave_request", request.id),
        )
    if await email_wanted(session, user_id, kind):
        await enqueue(
            session,
            user_id=user_id,
            template=EmailTemplate.LEAVE_REQUEST,
            idempotency_key=key,
            now=now,
            data=data,
        )


async def to_manager(
    session: Session, request: LeaveRequest, leave_type: str, event: str, *, now: datetime, today: date
) -> None:
    facts = await people.job_facts(session, request.employee_id, today)
    if facts is None or facts.manager_employee_id is None:
        return
    await _send(
        session,
        user_id=await access.manager_account(
            session, await identity.account_for_employee(session, facts.manager_employee_id), now
        ),
        request=request,
        leave_type=leave_type,
        event=event,
        now=now,
    )


async def to_employee(
    session: Session, request: LeaveRequest, leave_type: str, event: str, now: datetime
) -> None:
    await _send(
        session,
        user_id=await identity.account_for_employee(session, request.employee_id),
        request=request,
        leave_type=leave_type,
        event=event,
        now=now,
    )


async def recorded(session: Session, request: LeaveRequest, leave_type: str, now: datetime) -> None:
    await to_employee(session, request, leave_type, "recorded", now)
