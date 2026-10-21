"""In-app notifications and notification preferences (product-requirements NTF-1 to NTF-3).

- Other modules call `notify` in their own transaction, so a notification exists exactly when
  the change that caused it commits. The row carries a short title, a short body and an in-app
  link built from non-sensitive display values (a leave type, dates, a person's name): never a
  reason, a note, a document's content, an address or a secret (NTF-2). `idempotency_key`
  keeps one notification per event and recipient.
- Each notification type can be muted per channel (`in_app`, `email`) by its recipient
  (NTF-3). A missing preference means on. Security emails (sign-in, MFA, password, elevation)
  are not notification types here and can never be muted.
- People read, mark read and remove only their own notifications (`/me`); a notification of
  someone else is answered as not found.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from app.modules.notify.models import Notification, NotificationPreference
from app.platform.authz.context import Actor
from app.platform.clock import Clock
from app.platform.db import Database, Session
from app.platform.errors import ProblemError, ProblemType
from app.platform.ids import uuid7

MAX_TITLE: Final = 120
MAX_BODY: Final = 300
_LINK: Final = re.compile(r"/[A-Za-z0-9/_?=&.-]{0,199}")
_EMAIL_SHAPE: Final = re.compile(r"[^@\s]+@[^@\s]+")


class NotificationType(StrEnum):
    LEAVE_APPROVAL_REQUESTED = "leave.approval_requested"
    LEAVE_REQUEST_UPDATED = "leave.request_updated"
    CORRECTION_APPROVAL_REQUESTED = "attendance.correction_requested"
    CORRECTION_UPDATED = "attendance.correction_updated"
    DOCUMENT_ADDED = "document.added"
    EXPORT_READY = "export.ready"


class Channel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"


@dataclass(frozen=True, slots=True)
class TypeInfo:
    label: str
    description: str
    # The channels this type is delivered on; only those can be muted.
    channels: tuple[Channel, ...]


CATALOG: Final[dict[NotificationType, TypeInfo]] = {
    NotificationType.LEAVE_APPROVAL_REQUESTED: TypeInfo(
        "Leave waiting for my decision",
        "A team member asks for leave or to cancel approved leave.",
        (Channel.IN_APP, Channel.EMAIL),
    ),
    NotificationType.LEAVE_REQUEST_UPDATED: TypeInfo(
        "Updates to my leave",
        "Your leave is approved, not approved, cancelled or recorded.",
        (Channel.IN_APP, Channel.EMAIL),
    ),
    NotificationType.CORRECTION_APPROVAL_REQUESTED: TypeInfo(
        "Attendance corrections waiting for my decision",
        "A team member asks to correct their attendance.",
        (Channel.IN_APP, Channel.EMAIL),
    ),
    NotificationType.CORRECTION_UPDATED: TypeInfo(
        "Attendance correction updates",
        "A correction to your attendance is approved, not approved or recorded by HR, or HR "
        "corrects a team member's attendance.",
        (Channel.IN_APP, Channel.EMAIL),
    ),
    NotificationType.DOCUMENT_ADDED: TypeInfo(
        "Documents added for me",
        "HR adds a document to your record.",
        (Channel.IN_APP,),
    ),
    NotificationType.EXPORT_READY: TypeInfo(
        "Exports ready",
        "A report export you asked for is ready to download, or could not be produced.",
        (Channel.IN_APP,),
    ),
}


def _display(value: str, name: str, limit: int) -> str:
    """Display text, shortened to the limit (names and leave types vary in length)."""
    if not value or "\x00" in value or _EMAIL_SHAPE.search(value):
        raise ValueError(f"notification {name} must be display text without addresses")
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


async def _enabled(session: Session, user_id: uuid.UUID, kind: NotificationType, channel: Channel) -> bool:
    if channel not in CATALOG[kind].channels:
        return False
    stored = await session.find_one(
        NotificationPreference, {"user_id": user_id, "type": kind.value, "channel": channel.value}
    )
    return stored is None or stored.enabled


async def email_wanted(session: Session, user_id: uuid.UUID, kind: NotificationType) -> bool:
    """Whether the recipient wants this type by email (callers queue the email themselves)."""
    return await _enabled(session, user_id, kind, Channel.EMAIL)


async def notify(
    session: Session,
    *,
    user_id: uuid.UUID,
    kind: NotificationType,
    title: str,
    body: str | None,
    link: str | None,
    idempotency_key: str,
    subject: tuple[str, uuid.UUID] | None = None,
) -> bool:
    """Create an in-app notification in the caller's transaction, unless the recipient muted
    the type in the app. False if muted or already created for this event."""
    title = _display(title, "title", MAX_TITLE)
    if body is not None:
        body = _display(body, "body", MAX_BODY)
    if link is not None and not _LINK.fullmatch(link):
        raise ValueError("notification link must be an in-app path")
    if not 1 <= len(idempotency_key) <= 200:
        raise ValueError("idempotency_key must be 1 to 200 characters")
    if not await _enabled(session, user_id, kind, Channel.IN_APP):
        return False
    notification = Notification(
        recipient_user_id=user_id,
        type=kind.value,
        subject_type=subject[0] if subject else None,
        subject_id=subject[1] if subject else None,
        title=title,
        body=body,
        link=link,
        idempotency_key=idempotency_key,
    )
    return await session.insert_if_absent(notification, ["idempotency_key"])


@dataclass(frozen=True, slots=True)
class Page:
    items: list[Notification]
    unread: int
    next_after: tuple[datetime, uuid.UUID] | None


@dataclass(frozen=True, slots=True)
class Preference:
    kind: NotificationType
    info: TypeInfo
    enabled: dict[Channel, bool]


class InboxService:
    """The signed-in person's own notifications and preferences (`/me`)."""

    def __init__(self, *, database: Database, clock: Clock) -> None:
        self._db = database
        self._clock = clock

    async def unread_count(self, actor: Actor) -> int:
        async with self._db.unit_of_work() as session:
            return await self._unread(session, actor.user_id)

    @staticmethod
    async def _unread(session: Session, user_id: uuid.UUID) -> int:
        return await session.count(
            Notification, {"recipient_user_id": user_id, "deleted_at": None, "read_at": None}
        )

    async def page(
        self, actor: Actor, *, unread_only: bool, after: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> Page:
        query: dict[str, Any] = {"recipient_user_id": actor.user_id, "deleted_at": None}
        if unread_only:
            query["read_at"] = None
        if after is not None:
            query["$or"] = [
                {"created_at": {"$lt": after[0]}},
                {"created_at": after[0], "id": {"$lt": after[1]}},
            ]
        async with self._db.unit_of_work() as session:
            rows = await session.find(
                Notification, query, sort=[("created_at", -1), ("id", -1)], limit=limit + 1
            )
            unread = await self._unread(session, actor.user_id)
        last = rows[limit - 1] if len(rows) > limit else None
        return Page(rows[:limit], unread, (last.created_at, last.id) if last else None)

    async def mark_read(self, actor: Actor, notification_id: uuid.UUID) -> Notification:
        async with self._db.unit_of_work() as session:
            row = await session.get(Notification, notification_id, for_update=True)
            if row is None or row.recipient_user_id != actor.user_id or row.deleted_at is not None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if row.read_at is None:
                row.read_at = self._clock.now()
            await session.flush()
            return row

    async def mark_all_read(self, actor: Actor) -> int:
        async with self._db.unit_of_work() as session:
            return await session.update_many(
                Notification,
                {"recipient_user_id": actor.user_id, "deleted_at": None, "read_at": None},
                {"$set": {"read_at": self._clock.now()}},
            )

    async def remove(self, actor: Actor, notification_id: uuid.UUID) -> None:
        async with self._db.unit_of_work() as session:
            row = await session.get(Notification, notification_id, for_update=True)
            if row is None or row.recipient_user_id != actor.user_id or row.deleted_at is not None:
                raise ProblemError(ProblemType.NOT_FOUND)
            row.deleted_at = self._clock.now()
            row.read_at = row.read_at or row.deleted_at

    async def preferences(self, actor: Actor) -> list[Preference]:
        async with self._db.unit_of_work() as session:
            return await self._preferences(session, actor.user_id)

    @staticmethod
    async def _preferences(session: Session, user_id: uuid.UUID) -> list[Preference]:
        stored = {
            (row.type, row.channel): row.enabled
            for row in await session.find(NotificationPreference, {"user_id": user_id})
        }
        return [
            Preference(
                kind,
                info,
                {channel: stored.get((kind.value, channel.value), True) for channel in info.channels},
            )
            for kind, info in CATALOG.items()
        ]

    async def set_preferences(
        self, actor: Actor, changes: list[tuple[NotificationType, Channel, bool]]
    ) -> list[Preference]:
        for kind, channel, _ in changes:
            if channel not in CATALOG[kind].channels:
                raise ProblemError(
                    ProblemType.VALIDATION_ERROR,
                    extensions={
                        "errors": [
                            {
                                "field": "channel",
                                "code": "notification.channel_unavailable",
                                "message": "This notification is not sent on that channel.",
                            }
                        ]
                    },
                )
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            for kind, channel, enabled in changes:
                await session.find_one_and_update(
                    NotificationPreference,
                    {"user_id": actor.user_id, "type": kind.value, "channel": channel.value},
                    {"$set": {"enabled": enabled, "updated_at": now}, "$setOnInsert": {"_id": uuid7()}},
                    upsert=True,
                )
            return await self._preferences(session, actor.user_id)
