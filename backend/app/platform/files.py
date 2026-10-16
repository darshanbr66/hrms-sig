"""Private object storage for documents and export files (docs/security-architecture.md §6,
product-requirements DOC-3, RPT-3).

- One private bucket; objects are named by random keys under a prefix (`documents/`,
  `exports/`), never by anything a person typed. Nothing in it is public: the bucket has no
  public policy, and objects are reached only through presigned URLs.
- A download is a presigned GET valid for 60 seconds (rule 9), issued by the API after it has
  authorized and audited the request. The URL forces a download with the stored file name.
- Locally and in tests the bucket is on RustFS (`infra/compose.yaml`).

boto3 is synchronous, so each call runs in a worker thread. Errors carry only the S3 error
code, never request details.
"""

import asyncio
from typing import Any, Final, Protocol
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import SecretStr

DOWNLOAD_SECONDS: Final = 60


class FileStoreError(RuntimeError):
    """The private bucket could not be read or written."""


class FileStore(Protocol):
    async def put(self, key: str, body: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def download_url(self, key: str, *, filename: str, content_type: str) -> str: ...


def _error(exc: Exception) -> FileStoreError:
    if isinstance(exc, ClientError):
        return FileStoreError(
            f"file storage refused the request: {exc.response.get('Error', {}).get('Code')}"
        )
    return FileStoreError(f"file storage is unavailable: {type(exc).__name__}")


def attachment_header(filename: str) -> str:
    """`Content-Disposition: attachment` with the name encoded (RFC 6266, RFC 5987)."""
    ascii_name = "".join(c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


class S3FileStore:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key_id: SecretStr,
        secret_access_key: SecretStr,
        endpoint_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id.get_secret_value(),
            aws_secret_access_key=secret_access_key.get_secret_value(),
            config=Config(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        await self._call(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ChecksumAlgorithm="SHA256",
        )

    async def get(self, key: str) -> bytes:
        response = await self._call(self._client.get_object, Bucket=self._bucket, Key=key)
        try:
            return await asyncio.to_thread(response["Body"].read)
        except (BotoCoreError, ClientError) as exc:
            raise _error(exc) from None

    async def delete(self, key: str) -> None:
        await self._call(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def download_url(self, key: str, *, filename: str, content_type: str) -> str:
        url: str = await self._call(
            self._client.generate_presigned_url,
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentDisposition": attachment_header(filename),
                "ResponseContentType": content_type,
            },
            ExpiresIn=DOWNLOAD_SECONDS,
        )
        return url

    async def _call(self, method: Any, *args: Any, **arguments: Any) -> Any:
        try:
            return await asyncio.to_thread(method, *args, **arguments)
        except (BotoCoreError, ClientError) as exc:
            raise _error(exc) from None
