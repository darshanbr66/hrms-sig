"""Create the local audit anchor bucket on the Compose RustFS emulator (local and test only).

    uv run --env-file ../.env python -m scripts.create_local_anchor_bucket [--days N]

Creates `AUDIT_ANCHOR_BUCKET` with S3 Object Lock and a default retention in compliance mode
(`--days`, default 3650), so every anchor is write-once from the moment it is written. Running
it again changes nothing. In staging and production the bucket is created by infrastructure
with the audit retention plus one year (docs/security-architecture.md §8.1); this script
refuses to run there.
"""

import argparse
import os
import sys
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOCAL_ENVIRONMENTS = ("local", "test")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.create_local_anchor_bucket")
    parser.add_argument("--days", type=int, default=3650)
    days = parser.parse_args().days
    if os.environ.get("APP_ENV") not in LOCAL_ENVIRONMENTS:
        sys.stderr.write("Refused: this script is for local development only (APP_ENV local or test).\n")
        return 1
    bucket = os.environ["AUDIT_ANCHOR_BUCKET"]
    client: Any = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        region_name=os.environ["S3_REGION"],
        aws_access_key_id=os.environ["AUDIT_ANCHOR_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AUDIT_ANCHOR_SECRET_ACCESS_KEY"],
        config=Config(s3={"addressing_style": "path"}),
    )
    try:
        client.head_bucket(Bucket=bucket)
        sys.stdout.write(f"Bucket {bucket} already exists.\n")
    except ClientError:
        client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        client.put_object_lock_configuration(
            Bucket=bucket,
            ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": days}},
            },
        )
        sys.stdout.write(f"Created {bucket} with Object Lock, compliance mode, {days} days.\n")
    rule = client.get_object_lock_configuration(Bucket=bucket)["ObjectLockConfiguration"].get("Rule", {})
    retention = rule.get("DefaultRetention", {})
    if retention.get("Mode") != "COMPLIANCE":
        sys.stderr.write(f"Bucket {bucket} has no compliance-mode default retention; recreate it.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
