"""The leave module's interface for other modules and the worker (docs/architecture.md §4).

- `LeaveEmails` renders the leave request email at send time (notify.public.Renderer).
"""

from app.modules.leave.emails import LeaveEmails

__all__ = ["LeaveEmails"]
