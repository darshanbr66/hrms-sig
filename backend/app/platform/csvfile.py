"""CSV files for exports, safe to open in a spreadsheet (product-requirements RPT-3).

Text cells that a spreadsheet would read as a formula (starting with `=`, `+`, `-`, `@`, a tab
or a carriage return) are prefixed with an apostrophe (OWASP CSV injection guidance). Numbers,
dates and booleans are written as values. The file is UTF-8 with a byte order mark, so
spreadsheet programs read names in any script correctly.
"""

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")

type Cell = str | int | Decimal | bool | date | datetime | UUID | None


def safe_cell(value: Cell) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def to_csv(header: Sequence[str], rows: Iterable[Sequence[Cell]]) -> tuple[bytes, int]:
    """The file's bytes and its number of data rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([safe_cell(name) for name in header])
    count = 0
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
        count += 1
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"), count
