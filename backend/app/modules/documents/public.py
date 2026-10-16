"""The documents module's interface for the worker (docs/architecture.md §4).

- `register_document_scan` adds the every-minute virus scan of uploaded versions.
"""

from app.modules.documents.scan_job import register_document_scan

__all__ = ["register_document_scan"]
