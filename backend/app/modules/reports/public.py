"""The reports module's interface for the worker (docs/architecture.md §4).

- `register_exports` adds the every-minute production and removal of export files.
"""

from app.modules.reports.job import register_exports

__all__ = ["register_exports"]
