"""Write-once storage for audit checkpoint anchors (docs/security-architecture.md §8.1).

Each checkpoint is copied as a small JSON object into a bucket with S3 Object Lock in
compliance mode: once written, an object version cannot be deleted or overwritten, and its
retention cannot be shortened, by anyone, including the account owner, until it expires
(audit retention plus one year, the bucket's default retention, set by infrastructure).
Writing the same key again only adds a version; verification compares every version.

The anchor credentials should allow only writing objects and reading objects, versions and
their retention; never deleting or changing retention. Locally and in tests, the bucket is
on RustFS (`infra/compose.yaml`).

boto3 is synchronous, so each call runs in a worker thread. Errors carry only the S3 error
code, never request details.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import SecretStr


class AnchorStoreError(RuntimeError):
    """The anchor bucket could not be read or written."""


@dataclass(frozen=True, slots=True)
class AnchorVersion:
    version_id: str
    body: bytes
    retention_mode: str | None
    retain_until: datetime | None


@dataclass(frozen=True, slots=True)
class AnchorState:
    versions: Sequence[AnchorVersion]
    delete_markers: int


class AnchorStore(Protocol):
    async def put(self, key: str, body: bytes) -> None: ...

    async def state(self, key: str) -> AnchorState: ...

    async def keys(self, prefix: str, *, start_after: str | None = None) -> list[str]: ...


def _error(exc: Exception) -> AnchorStoreError:
    if isinstance(exc, ClientError):
        return AnchorStoreError(
            f"anchor storage refused the request: {exc.response.get('Error', {}).get('Code')}"
        )
    return AnchorStoreError(f"anchor storage is unavailable: {type(exc).__name__}")


class S3AnchorStore:
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
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=20,
            ),
        )

    async def put(self, key: str, body: bytes) -> None:
        await self._call(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            # Object Lock buckets need an integrity checksum on every write.
            ChecksumAlgorithm="SHA256",
        )

    async def state(self, key: str) -> AnchorState:
        return await asyncio.to_thread(self._state, key)

    async def keys(self, prefix: str, *, start_after: str | None = None) -> list[str]:
        return await asyncio.to_thread(self._keys, prefix, start_after)

    def _state(self, key: str) -> AnchorState:
        try:
            versions: list[AnchorVersion] = []
            delete_markers = 0
            paginator = self._client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=key):
                delete_markers += sum(1 for marker in page.get("DeleteMarkers", []) if marker["Key"] == key)
                for version in page.get("Versions", []):
                    if version["Key"] != key:
                        continue
                    version_id = version["VersionId"]
                    body = self._client.get_object(Bucket=self._bucket, Key=key, VersionId=version_id)[
                        "Body"
                    ].read()
                    mode, until = self._retention(key, version_id)
                    versions.append(AnchorVersion(version_id, body, mode, until))
            return AnchorState(versions, delete_markers)
        except (BotoCoreError, ClientError) as exc:
            raise _error(exc) from None

    def _retention(self, key: str, version_id: str) -> tuple[str | None, datetime | None]:
        try:
            retention = self._client.get_object_retention(
                Bucket=self._bucket, Key=key, VersionId=version_id
            ).get("Retention", {})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (
                "NoSuchObjectLockConfiguration",
                "InvalidRequest",
            ):
                return None, None
            raise
        return retention.get("Mode"), retention.get("RetainUntilDate")

    def _keys(self, prefix: str, start_after: str | None) -> list[str]:
        try:
            found: list[str] = []
            paginator = self._client.get_paginator("list_objects_v2")
            arguments: dict[str, str] = {"Bucket": self._bucket, "Prefix": prefix}
            if start_after is not None:
                arguments["StartAfter"] = start_after
            for page in paginator.paginate(**arguments):
                found.extend(item["Key"] for item in page.get("Contents", []))
            return found
        except (BotoCoreError, ClientError) as exc:
            raise _error(exc) from None

    async def _call(self, method: Any, **arguments: Any) -> Any:
        try:
            return await asyncio.to_thread(method, **arguments)
        except (BotoCoreError, ClientError) as exc:
            raise _error(exc) from None
