"""The grant request email, built by the worker at send time (notify.public.Renderer).

The email says what happened and points to the access requests page; the page shows who and
why to people allowed to see it. Role names come from the system role definitions.
"""

from collections.abc import Mapping

from app.modules.identity import public as identity
from app.modules.notify.public import EmailTemplate, OutboxItem, Renderer
from app.platform.authz.roles import ROLE_DEFINITIONS, RoleKey
from app.platform.db import Session
from app.platform.email import OutgoingEmail

PRODUCT = "Sigvitas HRMS"
SIGN_OFF = f"\n\n{PRODUCT}\nThis is an automatic message. Replies are not read."


def _role_name(key: object) -> str:
    try:
        return ROLE_DEFINITIONS[RoleKey(str(key))].name
    except ValueError:
        return str(key)


def _duration(minutes: object) -> str:
    if not isinstance(minutes, int):
        return ""
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'' if hours == 1 else 's'}"
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


def message(data: Mapping[str, object]) -> tuple[str, str] | None:
    """Subject and first paragraph for one event, or None for an unknown event."""
    event, kind = data.get("event"), data.get("kind")
    role, duration = _role_name(data.get("role")), _duration(data.get("minutes"))
    if kind == "super_admin_assignment":
        texts = {
            "requested": (
                "A super admin assignment needs approval",
                "A super admin asked to give the super admin role to an account. A second super admin "
                "who is neither of the two people involved must approve or reject it within 24 hours.",
            ),
            "approved": ("A super admin was added", "A request to give the super admin role was approved."),
            "rejected": (
                "A super admin assignment was rejected",
                "A request to give the super admin role was rejected.",
            ),
            "revoked": (
                "A super admin assignment was withdrawn",
                "A request to give the super admin role was withdrawn.",
            ),
            "expired": (
                "A super admin assignment expired",
                "A request to give the super admin role was not decided within 24 hours and has expired.",
            ),
        }
    elif kind == "break_glass":
        texts = {
            "break_glass": (
                "Break-glass access was used",
                f"A super admin used break-glass to give themselves the {role} role for {duration}, without "
                "approval. Another super admin must review this use.",
            ),
            "revoked": (
                "Break-glass access was ended",
                f"Break-glass access to the {role} role was ended early.",
            ),
            "ended": ("Break-glass access has ended", f"Break-glass access to the {role} role has ended."),
        }
    else:
        texts = {
            "requested": (
                "An elevation request needs approval",
                f"A super admin asked for temporary {role} access for {duration}. Another super admin "
                "must approve or reject it within 24 hours.",
            ),
            "approved": (
                "An elevation was approved",
                f"A request for temporary {role} access was approved. It ends by itself after {duration}.",
            ),
            "rejected": ("An elevation was rejected", f"A request for temporary {role} access was rejected."),
            "revoked": (
                "An elevation was ended or withdrawn",
                f"Temporary {role} access was ended early, or its request was withdrawn.",
            ),
            "ended": ("An elevation has ended", f"Temporary {role} access has ended."),
            "expired": (
                "An elevation request expired",
                f"A request for temporary {role} access was not decided within 24 hours and has expired.",
            ),
        }
    return texts.get(str(event))


class AccessEmails:
    def __init__(self, *, app_base_url: str) -> None:
        self._base = app_base_url

    def renderers(self) -> Mapping[EmailTemplate, Renderer]:
        return {EmailTemplate.GRANT_REQUEST: self.grant_request}

    async def grant_request(self, session: Session, item: OutboxItem) -> OutgoingEmail | None:
        address = await identity.active_email(session, item.user_id)
        text = message(item.data)
        if address is None or text is None:
            return None
        subject, paragraph = text
        return OutgoingEmail(
            to=address,
            subject=f"{subject} ({PRODUCT})",
            body=(
                f"Hello,\n\n{paragraph}\n\nSee the details on the access requests page:\n"
                f"{self._base}/admin/access-requests\n\nEvery step is recorded in the audit log.{SIGN_OFF}"
            ),
        )
