"""The documents module's part of permanently erasing one person's data (owner decision
2026-10-23, docs/database-design.md §9.1).

Every file of the person's documents is removed: their versions are marked erased in the
caller's transaction, so they can never be downloaded or scanned again, and their storage keys
go to the worker, which alone may remove stored files. Titles and file names are overwritten
with the erasure marker. The document and version records stay as the record of what was held
and when, with no content. Company documents the person uploaded are the company's and stay.
"""

import uuid
from datetime import datetime

from app.modules.documents.models import Document, DocumentVersion, Status
from app.platform.db import ERASED, Session
from app.platform.erasure import Erased, Outcome


async def erase_employee(session: Session, employee_id: uuid.UUID, now: datetime) -> Erased:
    result = Erased()
    documents = await session.find(Document, {"owner_employee_id": employee_id})
    for document in documents:
        document.title = ERASED
        document.status = Status.ARCHIVED.value
        document.updated_at = now
        document.version += 1
    ids = [document.id for document in documents]
    versions = await session.find(DocumentVersion, {"document_id": {"$in": ids}}) if ids else []
    erased = [version for version in versions if version.erased_at is None]
    for version in erased:
        version.original_filename = ERASED
        version.erased_at = now
    await session.flush()
    result.subject_ids.extend(ids)
    result.storage_keys.extend(version.storage_key for version in erased)
    result.add(
        "documents.files",
        Outcome.PENDING,
        len(erased),
        "The stored files of every version. The worker removes them; the request is complete only once "
        "none is left.",
    )
    result.add(
        "documents.titles_and_file_names",
        Outcome.ANONYMIZED,
        len(documents) + len(erased),
        "Document titles and the names the files were uploaded with.",
    )
    result.add(
        "documents.records",
        Outcome.RETAINED,
        len(documents) + len(versions),
        "Category, dates, size, checksum and virus-check result of each document and version, with no "
        "content: the record of what was held.",
    )
    return result
