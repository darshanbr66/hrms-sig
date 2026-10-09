"""Each process entry point (CLI, API, worker) runs in its own interpreter, so each must
register every table itself."""

import subprocess
import sys

import pytest

# Resolving each foreign key's target column fails when the target table was never imported.
CHECK = """
import {module}
from app.platform.db import Base
for table in Base.metadata.tables.values():
    for foreign_key in table.foreign_keys:
        foreign_key.column
"""


@pytest.mark.parametrize("module", ["app.cli", "app.main", "app.worker"])
def test_entry_point_registers_every_foreign_key_target(module: str) -> None:
    # The command is a fixed script run by this interpreter; nothing comes from outside.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CHECK.format(module=module)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
