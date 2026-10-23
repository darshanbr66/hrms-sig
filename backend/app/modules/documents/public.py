"""The documents module's interface for the worker and other modules (docs/architecture.md §4).

- `register_document_scan` adds the every-minute virus scan of uploaded versions.
- `erase_employee` is the documents part of the permanent erasure of one person's data.
"""

from app.modules.documents.erasure import erase_employee
from app.modules.documents.scan_job import register_document_scan

__all__ = ["erase_employee", "register_document_scan"]
