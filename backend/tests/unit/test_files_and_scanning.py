"""Upload checks, safe CSV cells, the ClamAV reply parser and download limits (DOC-5, RPT-3)."""

import asyncio
import struct
from datetime import timedelta

import pytest

from app.modules.documents.service import DOWNLOADS_PER_USER, safe_filename, sniff
from app.modules.reports.service import EXPORT_DOWNLOADS_PER_USER, EXPORTS_PER_USER
from app.platform.csvfile import safe_cell, to_csv
from app.platform.errors import ProblemError
from app.platform.files import attachment_header
from app.platform.ratelimit import FailureMode
from app.platform.scanning import ClamdScanner, ScanError, Verdict, parse_reply


def test_the_type_comes_from_the_content() -> None:
    assert sniff(b"%PDF-1.7 ...") == ("application/pdf", ".pdf")
    assert sniff(b"\x89PNG\r\n\x1a\n...") == ("image/png", ".png")
    assert sniff(b"\xff\xd8\xff\xe0...") == ("image/jpeg", ".jpg")
    with pytest.raises(ProblemError):
        sniff(b"MZ\x90\x00 an executable named invoice.pdf")


def test_file_names_lose_paths_and_reserved_characters() -> None:
    assert safe_filename("../../etc/passwd", ".pdf") == "passwd.pdf"
    assert safe_filename("C:\\Users\\x\\scan<1>.PDF", ".pdf") == "scan_1_.PDF"
    assert safe_filename(None, ".png") == "document.png"
    assert safe_filename("photo.jpeg", ".jpg") == "photo.jpeg"
    assert 'filename="' in attachment_header('résumé "final".pdf')


def test_csv_cells_cannot_become_formulas() -> None:
    for dangerous in ("=1+1", "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\tx", "\rx"):
        assert safe_cell(dangerous).startswith("'")
    assert safe_cell("Test person") == "Test person"
    assert safe_cell(-1) == "-1"
    body, rows = to_csv(("name",), [['=HYPERLINK("x")'], ["Plain"]])
    assert rows == 2
    assert body.startswith(b"\xef\xbb\xbf")
    assert b"'=HYPERLINK" in body


def test_scanner_replies() -> None:
    assert parse_reply(b"stream: OK\0").verdict is Verdict.CLEAN
    infected = parse_reply(b"stream: Eicar-Test-Signature FOUND\0")
    assert infected.verdict is Verdict.INFECTED
    assert infected.signature == "Eicar-Test-Signature"
    with pytest.raises(ScanError):
        parse_reply(b"INSTREAM size limit exceeded. ERROR\0")


async def test_the_scanner_speaks_instream() -> None:
    received = bytearray()

    async def clamd(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readexactly(10) == b"zINSTREAM\0"
        while True:
            (length,) = struct.unpack("!L", await reader.readexactly(4))
            if length == 0:
                break
            received.extend(await reader.readexactly(length))
        writer.write(b"stream: OK\0")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(clamd, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        result = await ClamdScanner("127.0.0.1", port).scan(b"x" * 100_000)
    assert result.verdict is Verdict.CLEAN
    assert len(received) == 100_000


async def test_an_unreachable_scanner_is_an_error_never_clean() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(ScanError):
        await ClamdScanner("127.0.0.1", port).scan(b"data")


def test_download_and_export_limits_fail_closed() -> None:
    """api-architecture §9: downloads 60 and exports 5 per user per hour; when Redis cannot
    be asked, they are refused (503) rather than allowed (architecture §3.1)."""
    assert (DOWNLOADS_PER_USER.limit, DOWNLOADS_PER_USER.window) == (60, timedelta(hours=1))
    assert (EXPORT_DOWNLOADS_PER_USER.limit, EXPORT_DOWNLOADS_PER_USER.window) == (60, timedelta(hours=1))
    assert (EXPORTS_PER_USER.limit, EXPORTS_PER_USER.window) == (5, timedelta(hours=1))
    for rule in (DOWNLOADS_PER_USER, EXPORT_DOWNLOADS_PER_USER, EXPORTS_PER_USER):
        assert rule.scope.failure_mode is FailureMode.CLOSED
