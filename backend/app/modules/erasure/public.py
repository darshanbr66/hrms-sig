"""The erasure module's interface for the worker (docs/architecture.md §4).

- `register_erasure_files` adds the every-minute removal of an erasure's stored files.
"""

from app.modules.erasure.job import register_erasure_files

__all__ = ["register_erasure_files"]
