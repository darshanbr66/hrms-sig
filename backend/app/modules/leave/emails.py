"""The leave request email, built by the worker at send time (notify.public.Renderer).

It says what happened, with the leave type and dates, and links to the request. The reason and
any decision note stay in the application, where only the people allowed to see them can.
"""

from collections.abc import Mapping

from app.modules.identity import public as identity
from app.modules.notify.public import EmailTemplate, OutboxItem, Renderer
from app.platform.db import Session
from app.platform.email import OutgoingEmail

PRODUCT = "Sigvitas HRMS"
SIGN_OFF = f"\n\n{PRODUCT}\nThis is an automatic message. Replies are not read."


def message(data: Mapping[str, object]) -> tuple[str, str, str] | None:
    """Subject, first paragraph and link path for one event, or None for an unknown event."""
    leave_type, start, end, days = (
        data.get("leave_type"),
        data.get("start_date"),
        data.get("end_date"),
        data.get("days"),
    )
    request_id = data.get("request_id")
    span = f"{leave_type}, {start} to {end} ({days} days)"
    texts = {
        "submitted": (
            "A leave request needs your decision",
            f"A member of your team asked for leave: {span}.",
            "/leave/approvals",
        ),
        "cancellation_requested": (
            "A leave cancellation needs your decision",
            f"A member of your team asked to cancel approved leave: {span}.",
            "/leave/approvals",
        ),
        "approved": (
            "Your leave was approved",
            f"Your leave request was approved: {span}.",
            f"/leave/requests/{request_id}",
        ),
        "rejected": (
            "Your leave request was not approved",
            f"Your leave request was not approved: {span}. The request shows why.",
            f"/leave/requests/{request_id}",
        ),
        "cancellation_approved": (
            "Your leave was cancelled",
            f"Your request to cancel leave was approved: {span}. The days are back in your balance.",
            f"/leave/requests/{request_id}",
        ),
        "cancellation_rejected": (
            "Your leave stays booked",
            f"Your request to cancel leave was not approved, so the leave stays booked: {span}.",
            f"/leave/requests/{request_id}",
        ),
        "recorded": (
            "Your leave is recorded",
            f"Your leave is recorded: {span}.",
            f"/leave/requests/{request_id}",
        ),
    }
    return texts.get(str(data.get("event")))


class LeaveEmails:
    def __init__(self, *, app_base_url: str) -> None:
        self._base = app_base_url.rstrip("/")

    def renderers(self) -> Mapping[EmailTemplate, Renderer]:
        return {EmailTemplate.LEAVE_REQUEST: self.leave_request}

    async def leave_request(self, session: Session, item: OutboxItem) -> OutgoingEmail | None:
        address = await identity.active_email(session, item.user_id)
        text = message(item.data)
        if address is None or text is None:
            return None
        subject, body, path = text
        return OutgoingEmail(
            to=address,
            subject=f"{subject} ({PRODUCT})",
            body=f"Hello,\n\n{body}\n\nOpen it in the HRMS:\n{self._base}{path}{SIGN_OFF}",
        )
