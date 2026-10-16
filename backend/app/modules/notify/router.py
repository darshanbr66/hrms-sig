"""HTTP routes for the signed-in person's notifications (docs/api-architecture.md §8,
Notifications). Self-service only: no route takes a user or employee ID (CLAUDE.md rule 3)."""

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.modules.notify.inbox import Channel, InboxService, NotificationType, Preference
from app.modules.notify.models import Notification
from app.platform.authz.context import Actor
from app.platform.authz.route import requires
from app.platform.pagination import decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1", tags=["notifications"])

MAX_LIMIT: Final = 100

type TypeValue = Literal[
    "leave.approval_requested",
    "leave.request_updated",
    "attendance.correction_requested",
    "attendance.correction_updated",
    "document.added",
    "export.ready",
]
type ChannelValue = Literal["in_app", "email"]


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotificationView(_Response):
    id: uuid.UUID
    type: TypeValue
    title: str
    body: str | None
    link: str | None
    read: bool
    created_at: datetime


class NotificationPage(_Response):
    items: list[NotificationView]
    unread_count: int
    next_cursor: str | None


class UnreadCount(_Response):
    unread_count: int


class ReadAllResult(_Response):
    marked_read: int


class PreferenceView(_Response):
    type: TypeValue
    label: str
    description: str
    # Channel -> on or off, for the channels this type is sent on.
    channels: dict[ChannelValue, bool]


class PreferenceList(_Response):
    items: list[PreferenceView]


class PreferenceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TypeValue
    channel: ChannelValue
    enabled: bool


class PreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[PreferenceChange] = Field(min_length=1, max_length=40)


def service(request: Request) -> InboxService:
    value: InboxService = request.app.state.inbox_service
    return value


Service = Annotated[InboxService, Depends(service)]


def _view(row: Notification) -> NotificationView:
    return NotificationView(
        id=row.id,
        type=row.type,
        title=row.title,
        body=row.body,
        link=row.link,
        read=row.read_at is not None,
        created_at=row.created_at,
    )


def _preferences(items: list[Preference]) -> PreferenceList:
    return PreferenceList(
        items=[
            PreferenceView(
                type=item.kind.value,
                label=item.info.label,
                description=item.info.description,
                channels={channel.value: on for channel, on in item.enabled.items()},
            )
            for item in items
        ]
    )


@router.get("/me/notifications", response_model=NotificationPage)
async def my_notifications(
    *,
    inbox: Service,
    actor: Annotated[Actor, requires("notification.read.self")],
    unread: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 30,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> NotificationPage:
    page = await inbox.page(actor, unread_only=unread, after=decode_cursor(cursor), limit=limit)
    return NotificationPage(
        items=[_view(row) for row in page.items],
        unread_count=page.unread,
        next_cursor=encode_cursor(page.next_after),
    )


@router.get("/me/notifications/unread-count", response_model=UnreadCount)
async def my_unread_count(
    inbox: Service, actor: Annotated[Actor, requires("notification.read.self")]
) -> UnreadCount:
    """For the header badge. Send `X-SV-Background: 1` when polling, so polling does not keep
    an idle session alive."""
    return UnreadCount(unread_count=await inbox.unread_count(actor))


@router.post("/me/notifications/read-all", response_model=ReadAllResult)
async def read_all_notifications(
    inbox: Service, actor: Annotated[Actor, requires("notification.read.self")]
) -> ReadAllResult:
    return ReadAllResult(marked_read=await inbox.mark_all_read(actor))


@router.post("/me/notifications/{notification_id}/read", response_model=NotificationView)
async def read_notification(
    notification_id: uuid.UUID, inbox: Service, actor: Annotated[Actor, requires("notification.read.self")]
) -> NotificationView:
    return _view(await inbox.mark_read(actor, notification_id))


@router.delete("/me/notifications/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_notification(
    notification_id: uuid.UUID, inbox: Service, actor: Annotated[Actor, requires("notification.read.self")]
) -> Response:
    await inbox.remove(actor, notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/notification-preferences", response_model=PreferenceList)
async def my_notification_preferences(
    inbox: Service, actor: Annotated[Actor, requires("notification.read.self")]
) -> PreferenceList:
    return _preferences(await inbox.preferences(actor))


@router.put("/me/notification-preferences", response_model=PreferenceList)
async def update_notification_preferences(
    body: PreferenceUpdate,
    inbox: Service,
    actor: Annotated[Actor, requires("notification.preference.update.self")],
) -> PreferenceList:
    """Turn notification types on or off per channel. Security emails are not listed and are
    always sent."""
    changes = [(NotificationType(c.type), Channel(c.channel), c.enabled) for c in body.changes]
    return _preferences(await inbox.set_preferences(actor, changes))
