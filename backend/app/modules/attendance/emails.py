"""The attendance correction email, built by the worker at send time (notify.public.Renderer).

It says what happened and the work date, and links to the correction. The reason and any
decision note stay in the application, where only the people allowed to see them can.
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
    work_date = data.get("work_date")
    correction_id = data.get("correction_id")
    link = f"/attendance/corrections/{correction_id}"
    texts = {
        "submitted": (
            "An attendance correction needs your decision",
            f"A member of your team asked to correct their attendance for {work_date}.",
            link,
        ),
        "approved": (
            "Your attendance correction was approved",
            f"Your correction to your attendance for {work_date} was approved.",
            link,
        ),
        "rejected": (
            "Your attendance correction was not approved",
            f"Your correction to your attendance for {work_date} was not approved. The correction shows why.",
            link,
        ),
        "recorded": (
            "HR corrected your attendance",
            f"HR recorded a correction to your attendance for {work_date}.",
            link,
        ),
        "recorded_for_team": (
            "HR corrected a team member's attendance",
            f"HR recorded a correction to a team member's attendance for {work_date}.",
            link,
        ),
    }
    return texts.get(str(data.get("event")))


class AttendanceEmails:
    def __init__(self, *, app_base_url: str) -> None:
        self._base = app_base_url.rstrip("/")

    def renderers(self) -> Mapping[EmailTemplate, Renderer]:
        return {EmailTemplate.ATTENDANCE_CORRECTION: self.correction}

    async def correction(self, session: Session, item: OutboxItem) -> OutgoingEmail | None:
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
