"""The notify module's part of permanently erasing one person's data (owner decision
2026-10-23, docs/database-design.md §9.1).

The person's own notifications, notification preferences and emails (queued or sent) are
deleted, and so are other people's notifications about the person's leave requests, attendance
corrections and documents (a manager's "a member of your team asked for leave"). Emails already
delivered to other people's mailboxes are outside the system.
"""

import uuid
from collections.abc import Collection

from app.modules.notify.models import EmailOutbox, Notification, NotificationPreference
from app.platform.db import Session
from app.platform.erasure import Erased, Outcome


async def erase_for(
    session: Session, user_id: uuid.UUID | None, subject_ids: Collection[uuid.UUID]
) -> Erased:
    result = Erased()
    own = await session.delete_many(Notification, {"recipient_user_id": user_id}) if user_id else 0
    preferences = await session.delete_many(NotificationPreference, {"user_id": user_id}) if user_id else 0
    emails = await session.delete_many(EmailOutbox, {"user_id": user_id}) if user_id else 0
    about = (
        await session.delete_many(Notification, {"subject_id": {"$in": list(subject_ids)}})
        if subject_ids
        else 0
    )
    result.add(
        "notify.own_notifications",
        Outcome.DELETED,
        own + preferences,
        "In-app notifications and preferences.",
    )
    result.add(
        "notify.emails",
        Outcome.DELETED,
        emails,
        "Queued and sent email records addressed to the account. Emails already delivered are in the "
        "recipient's mailbox, outside the system.",
    )
    result.add(
        "notify.notifications_about_the_person",
        Outcome.DELETED,
        about,
        "Other people's notifications about the person's leave requests, corrections and documents.",
    )
    return result
