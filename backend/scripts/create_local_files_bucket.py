"""Create the private documents and exports bucket on the Compose RustFS emulator (local and test only).

    uv run --env-file ../.env python -m scripts.create_local_files_bucket

Creates `FILES_BUCKET` with no bucket policy, so nothing in it can be read without credentials,
then checks that an anonymous request is refused. Running it again changes nothing. Each local
database needs its own files bucket, as it needs its own anchor bucket (docs/local-development.md
§3.2). In staging and production the bucket is created by infrastructure (private, encrypted, no
public access); this script refuses to run there. It prints only the bucket name.
"""

import os
import sys
from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

LOCAL_ENVIRONMENTS = ("local", "test")


def anonymous_read_refused(endpoint: str, region: str, bucket: str) -> bool:
    """True when listing the bucket without credentials is refused."""
    anonymous: Any = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}),
    )
    try:
        anonymous.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except ClientError as exc:
        return exc.response.get("Error", {}).get("Code") in ("AccessDenied", "Forbidden", "403")
    return False


def main() -> int:
    if os.environ.get("APP_ENV") not in LOCAL_ENVIRONMENTS:
        sys.stderr.write("Refused: this script is for local development only (APP_ENV local or test).\n")
        return 1
    bucket = os.environ.get("FILES_BUCKET", "")
    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    if not bucket or not endpoint:
        sys.stderr.write("FILES_BUCKET and S3_ENDPOINT_URL must be set (docs/local-development.md §2).\n")
        return 1
    if bucket == os.environ.get("AUDIT_ANCHOR_BUCKET"):
        sys.stderr.write("FILES_BUCKET must differ from AUDIT_ANCHOR_BUCKET.\n")
        return 1
    client: Any = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ["S3_REGION"],
        aws_access_key_id=os.environ["FILES_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["FILES_SECRET_ACCESS_KEY"],
        config=Config(s3={"addressing_style": "path"}),
    )
    try:
        client.head_bucket(Bucket=bucket)
        sys.stdout.write(f"Bucket {bucket} already exists.\n")
    except ClientError:
        client.create_bucket(Bucket=bucket)
        sys.stdout.write(f"Created private bucket {bucket}.\n")
    try:
        client.get_bucket_policy(Bucket=bucket)
    except ClientError:
        pass
    else:
        sys.stderr.write(f"Bucket {bucket} has a bucket policy; documents must stay private. Remove it.\n")
        return 1
    if not anonymous_read_refused(endpoint, os.environ["S3_REGION"], bucket):
        sys.stderr.write(f"Bucket {bucket} answers anonymous requests; it must be private.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
