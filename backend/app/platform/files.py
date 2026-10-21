"""Private file storage for documents and export files in MongoDB GridFS (docs/security-architecture.md
§6, product-requirements DOC-3, RPT-3, ADR-030).

- Files live in the `files` GridFS bucket (`files.files`, `files.chunks`), named by random keys
  under a prefix (`documents/`, `exports/`), never by anything a person typed. Each file's
  metadata names its organization; a file is read or removed only within that organization.
- A download is a link valid for 60 seconds (rule 9), issued by the API after it has
  authorized and audited the request: `/api/v1/files/<token>`, where the token names the file,
  its organization, the download name and type, and the expiry, and is signed with
  `DOWNLOAD_LINK_HMAC_KEY`. The link route checks the signature and the expiry before it reads
  anything, and answers with `Content-Disposition: attachment`, `no-store` and a sandboxing CSP.
- What GridFS does not give: it is not write-once storage and not a backup. Files share the
  database's backups and its storage limit (0.5 GB on the Atlas free tier).

Errors carry only the error type, never file contents or names.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Final, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from gridfs import AsyncGridFSBucket
from gridfs.errors import NoFile
from pymongo.errors import PyMongoError

from app.platform.authz.route import public_route
from app.platform.db import Database, Index, current_tenant
from app.platform.errors import ProblemError, ProblemType

DOWNLOAD_SECONDS: Final = 60
BUCKET: Final = "files"
LINK_PREFIX: Final = "/api/v1/files/"
# The bucket's indexes, created by the migration command, so the API's database user needs no
# index privileges: GridFS's own two, and the organization lookup.
FILE_INDEXES: Final = {
    f"{BUCKET}.files": (
        Index((("filename", 1), ("uploadDate", 1)), name="filename_1_uploadDate_1"),
        Index((("metadata.organization_id", 1), ("filename", 1)), name="ix_files_organization_filename"),
    ),
    f"{BUCKET}.chunks": (Index((("files_id", 1), ("n", 1)), name="files_id_1_n_1", unique=True),),
}


class FileStoreError(RuntimeError):
    """File storage could not be read or written."""


class FileStore(Protocol):
    async def put(self, key: str, body: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def download_url(self, key: str, *, filename: str, content_type: str) -> str: ...


def attachment_header(filename: str) -> str:
    """`Content-Disposition: attachment` with the name encoded (RFC 6266, RFC 5987)."""
    ascii_name = "".join(c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class LinkSigner:
    """Signs and checks download tokens."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def sign(self, claims: dict[str, Any]) -> str:
        payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{signature}"

    def check(self, token: str, now: float) -> dict[str, Any] | None:
        payload, _, signature = token.partition(".")
        expected = _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())
        if not signature or not hmac.compare_digest(signature, expected):
            return None
        try:
            claims: dict[str, Any] = json.loads(_unb64(payload))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(claims.get("e"), int) or claims["e"] < now:
            return None
        return claims


class GridFsFileStore:
    """The `files` bucket, for the organization of the current scope."""

    def __init__(self, database: Database, signer: LinkSigner | None = None) -> None:
        self._bucket = AsyncGridFSBucket(database.db, bucket_name=BUCKET)
        self._files = database.collection(f"{BUCKET}.files")
        self._signer = signer

    @staticmethod
    def _organization() -> uuid.UUID:
        organization = current_tenant()
        if organization is None:
            raise FileStoreError("files are stored and read only inside an organization")
        return organization

    async def _find(self, key: str, organization: uuid.UUID) -> Any:
        try:
            return await self._files.find_one({"filename": key, "metadata.organization_id": organization})
        except PyMongoError as exc:
            raise FileStoreError(f"file storage is unavailable: {type(exc).__name__}") from None

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        organization = self._organization()
        try:
            await self._bucket.upload_from_stream(
                key, body, metadata={"organization_id": organization, "content_type": content_type}
            )
        except PyMongoError as exc:
            raise FileStoreError(f"file storage is unavailable: {type(exc).__name__}") from None

    async def get(self, key: str) -> bytes:
        return await self.read(key, self._organization())

    async def read(self, key: str, organization: uuid.UUID) -> bytes:
        stored = await self._find(key, organization)
        if stored is None:
            raise FileStoreError("file storage has no such file")
        try:
            stream = await self._bucket.open_download_stream(stored["_id"])
            return await stream.read()
        except (NoFile, PyMongoError) as exc:
            raise FileStoreError(f"file storage could not read the file: {type(exc).__name__}") from None

    async def delete(self, key: str) -> None:
        stored = await self._find(key, self._organization())
        if stored is None:
            return
        try:
            await self._bucket.delete(stored["_id"])
        except (NoFile, PyMongoError) as exc:
            raise FileStoreError(f"file storage could not remove the file: {type(exc).__name__}") from None

    async def download_url(self, key: str, *, filename: str, content_type: str) -> str:
        if self._signer is None:
            raise FileStoreError("this process cannot issue download links")
        claims = {
            "k": key,
            "o": str(self._organization()),
            "n": filename,
            "t": content_type,
            "e": int(time.time()) + DOWNLOAD_SECONDS,
        }
        return LINK_PREFIX + self._signer.sign(claims)


router = APIRouter(prefix="/api/v1/files", tags=["files"])


@router.get("/{token}", dependencies=[public_route()], include_in_schema=False)
async def download(token: str, request: Request) -> Response:
    """Serve a file named by a valid, unexpired download token (a 60-second link)."""
    store: GridFsFileStore = request.app.state.file_store
    signer: LinkSigner = request.app.state.link_signer
    claims = signer.check(token, time.time()) if len(token) <= 4096 else None
    if claims is None:
        raise ProblemError(ProblemType.NOT_FOUND)
    try:
        body = await store.read(str(claims["k"]), uuid.UUID(str(claims["o"])))
    except (FileStoreError, ValueError) as exc:
        raise ProblemError(ProblemType.NOT_FOUND) from exc
    return Response(
        content=body,
        media_type=str(claims["t"]),
        headers={
            "Content-Disposition": attachment_header(str(claims["n"])),
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )
