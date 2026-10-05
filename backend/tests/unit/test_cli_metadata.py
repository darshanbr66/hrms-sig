"""The operator CLI runs in its own process, so it must register every table itself."""

import subprocess
import sys

# Resolving each foreign key's target column fails when the target table was never imported.
CHECK = """
import app.cli
from app.platform.db import Base
for table in Base.metadata.tables.values():
    for foreign_key in table.foreign_keys:
        foreign_key.column
"""


def test_cli_registers_every_foreign_key_target() -> None:
    # The command is a fixed script run by this interpreter; nothing comes from outside.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CHECK], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
