"""Notifications about attendance corrections (product-requirements NTF-1, NTF-2; SOD-7),
written in the caller's transaction.

- A correction waiting for a decision reaches the employee's reporting manager on the day it
  is sent, if they have an active account; HR sees every waiting correction in the inbox.
- A decision reaches the employee.
- A correction HR records on someone's behalf reaches the employee and their manager.

Each is an in-app notification and an email, unless the recipient muted that channel. The
email and the notification carry the work date and a link: never the reason or a note.
"""

import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance.emails import message
from app.modules.attendance.models import AttendanceCorrection
from app.modules.identity import public as identity
from app.modules.notify.public import EmailTemplate, NotificationType, email_wanted, enqueue, notify
from app.modules.people import public as people

_FOR_MANAGER = frozenset({"submitted", "recorded_for_team"})


async def _send(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    correction: AttendanceCorrection,
    event: str,
    now: datetime,
) -> None:
    if user_id is None:
        return
    data: dict[str, str | int | bool | None] = {
        "event": event,
        "work_date": correction.work_date.isoformat(),
        "correction_id": str(correction.id),
    }
    text = message(data)
    if text is None:
        return
    kind = (
        NotificationType.CORRECTION_APPROVAL_REQUESTED
        if event == "submitted"
        else NotificationType.CORRECTION_UPDATED
    )
    key = f"attendance_correction:{correction.id}:{correction.version}:{event}:{user_id}"
    title, body, link = text
    if event in _FOR_MANAGER:
        name = (await people.employee_names(session, {correction.employee_id})).get(correction.employee_id)
        if name:
            body = body.replace("a team member's", f"{name}'s", 1).replace("A member of your team", name, 1)
    await notify(
        session,
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        link=link,
        idempotency_key=key,
        subject=("attendance_correction", correction.id),
    )
    if await email_wanted(session, user_id, kind):
        await enqueue(
            session,
            user_id=user_id,
            template=EmailTemplate.ATTENDANCE_CORRECTION,
            idempotency_key=key,
            now=now,
            data=data,
        )


async def to_manager(
    session: AsyncSession, correction: AttendanceCorrection, event: str, *, now: datetime, today: date
) -> None:
    facts = await people.job_facts(session, correction.employee_id, today)
    if facts is None or facts.manager_employee_id is None:
        return
    await _send(
        session,
        user_id=await identity.account_for_employee(session, facts.manager_employee_id),
        correction=correction,
        event=event,
        now=now,
    )


async def to_employee(
    session: AsyncSession, correction: AttendanceCorrection, event: str, now: datetime
) -> None:
    await _send(
        session,
        user_id=await identity.account_for_employee(session, correction.employee_id),
        correction=correction,
        event=event,
        now=now,
    )
