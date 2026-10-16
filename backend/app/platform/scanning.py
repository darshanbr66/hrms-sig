"""Malware scanning through a ClamAV daemon (product-requirements DOC-5).

The clamd `INSTREAM` command over TCP: the file is sent in length-prefixed chunks and the
daemon answers `stream: OK`, `stream: <signature> FOUND`, or an error. No dependency beyond
asyncio. Anything other than a clear answer is an error, so a document is never marked clean
by accident (fail closed).
"""

import asyncio
import struct
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

CHUNK: Final = 64 * 1024
TIMEOUT_SECONDS: Final = 60
MAX_REPLY: Final = 4096


class Verdict(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"


@dataclass(frozen=True, slots=True)
class ScanResult:
    verdict: Verdict
    # The signature name when infected (never shown to the uploader).
    signature: str | None = None


class ScanError(RuntimeError):
    """The scanner could not give an answer."""


class Scanner(Protocol):
    async def scan(self, data: bytes) -> ScanResult: ...


def parse_reply(reply: bytes) -> ScanResult:
    text = reply.rstrip(b"\0\n").decode("utf-8", "replace").strip()
    if text == "stream: OK":
        return ScanResult(Verdict.CLEAN)
    if text.startswith("stream: ") and text.endswith(" FOUND"):
        return ScanResult(Verdict.INFECTED, text.removeprefix("stream: ").removesuffix(" FOUND")[:200])
    raise ScanError("the scanner did not give a clear answer")


class ClamdScanner:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    async def scan(self, data: bytes) -> ScanResult:
        try:
            return await asyncio.wait_for(self._scan(data), TIMEOUT_SECONDS)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ScanError(f"the scanner is unavailable: {type(exc).__name__}") from None

    async def _scan(self, data: bytes) -> ScanResult:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(b"zINSTREAM\0")
            for offset in range(0, len(data), CHUNK):
                chunk = data[offset : offset + CHUNK]
                writer.write(struct.pack("!L", len(chunk)) + chunk)
                await writer.drain()
            writer.write(struct.pack("!L", 0))
            await writer.drain()
            reply = await reader.read(MAX_REPLY)
        finally:
            writer.close()
            await writer.wait_closed()
        return parse_reply(reply)
