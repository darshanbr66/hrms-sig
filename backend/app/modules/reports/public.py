"""The reports module's interface for the worker (docs/architecture.md §4).

- `register_exports` adds the every-minute production and removal of export files.
- `exports_held` counts, for the permanent erasure of a person's data, the export files that
  still exist: they are removed when they expire (24 hours).
"""

import uuid

from app.modules.reports.job import register_exports
from app.modules.reports.models import ExportJob, ExportStatus
from app.platform.db import Session
from app.platform.erasure import Erased, Outcome

__all__ = ["exports_held", "register_exports"]


async def exports_held(session: Session, user_id: uuid.UUID | None) -> Erased:
    result = Erased()
    unexpired = await session.count(ExportJob, {"status": ExportStatus.DONE.value})
    mine = (
        await session.count(ExportJob, {"requested_by": user_id, "status": ExportStatus.DONE.value})
        if user_id
        else 0
    )
    result.add(
        "reports.export_files",
        Outcome.RETAINED,
        unexpired,
        f"Export files that still exist ({mine} asked for by this account) may hold rows about the person. "
        "Each is removed automatically 24 hours after it was produced; nobody can download one after that.",
    )
    return result
