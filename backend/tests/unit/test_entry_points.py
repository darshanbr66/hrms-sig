"""Every entry point starts from a clean interpreter, and every stored model is declared.

A model missing from `app.metadata.MODELS` would get no collection validator and no indexes
(its unique rules would silently not apply), so the declaration list is compared with every
`Document` subclass the application defines.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import app.main
import app.worker
from app.metadata import MODELS
from app.platform.db import Document, Record

BACKEND = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module", ["app.main", "app.worker", "app.cli", "scripts.mongo_roles"])
def test_entry_point_imports_cleanly(module: str) -> None:
    result = subprocess.run(  # noqa: S603 (fixed arguments)
        [sys.executable, "-c", f"import {module}"], cwd=BACKEND, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _subclasses(cls: type[Document]) -> set[type[Document]]:
    found: set[type[Document]] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _subclasses(sub)
    return found


def test_every_stored_model_is_declared() -> None:
    assert app.main  # imported for their models
    assert app.worker
    stored = {cls for cls in _subclasses(Document) if cls is not Record and "__collection__" in cls.__dict__}
    assert stored - set(MODELS) == set()
    collections = [model.__collection__ for model in MODELS]
    assert len(collections) == len(set(collections))


def test_every_tenant_owned_unique_index_starts_with_the_organization() -> None:
    """A unique rule of one organization must not block another organization's records."""
    global_by_design = {"uq_users_email", "uq_session_tokens_token_hash", "uq_one_time_tokens_token_hash"}
    global_by_design |= {"uq_trusted_devices_token_hash", "uq_document_versions_storage_key"}
    global_by_design |= {"uq_export_jobs_storage_key"}
    for model in MODELS:
        if not model.__tenant_scoped__:
            continue
        for index in model.__indexes__:
            if index.unique and index.name not in global_by_design:
                assert index.keys[0][0] == "organization_id", (model.__collection__, index.name)
