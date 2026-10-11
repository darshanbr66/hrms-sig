"""Check that every process will use the intended local database and anchor bucket (local only).

    uv run --env-file ../.env python -m scripts.check_local_target --database NAME --bucket NAME

`run-backend.ps1` runs this before applying migrations. It stops (exit code 1) unless all four
`DATABASE_URL_*` settings name the same local database `NAME` on this machine, the migrator's
connection really lands in that database, and `AUDIT_ANCHOR_BUCKET` is `--bucket`. Each local
database needs its own anchor bucket (docs/local-development.md §3.2). It prints only the
database name, the applied Alembic revision and the bucket name, never a URL or credential.
"""

import argparse
import os
import sys
from urllib.parse import urlsplit

import psycopg

LOCAL_ENVIRONMENTS = ("local", "test")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
URL_SETTINGS = (
    "DATABASE_URL_APP",
    "DATABASE_URL_WORKER",
    "DATABASE_URL_MIGRATOR",
    "DATABASE_URL_AUDIT_RETENTION",
)


def _problems(database: str, bucket: str) -> list[str]:
    problems = []
    for name in URL_SETTINGS:
        value = os.environ.get(name)
        if not value:
            problems.append(f"{name} is not set.")
            continue
        parts = urlsplit(value)
        if parts.hostname not in LOCAL_HOSTS:
            problems.append(f"{name} does not point at a database on this machine.")
        if parts.path.lstrip("/") != database:
            problems.append(
                f"{name} names database {parts.path.lstrip('/') or '(none)'}, expected {database}."
            )
    actual_bucket = os.environ.get("AUDIT_ANCHOR_BUCKET", "")
    if actual_bucket != bucket:
        problems.append(f"AUDIT_ANCHOR_BUCKET is {actual_bucket or '(not set)'}, expected {bucket}.")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.check_local_target")
    parser.add_argument("--database", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()
    if os.environ.get("APP_ENV") not in LOCAL_ENVIRONMENTS:
        sys.stderr.write("Refused: this script is for local development only (APP_ENV local or test).\n")
        return 1
    problems = _problems(args.database, args.bucket)
    if problems:
        sys.stderr.write("Stopped: the settings do not match the intended target.\n")
        sys.stderr.writelines(f"  - {problem}\n" for problem in problems)
        return 1
    try:
        with psycopg.connect(
            os.environ["DATABASE_URL_MIGRATOR"], connect_timeout=5, application_name="hrms-preflight"
        ) as conn:
            row = conn.execute("SELECT current_database(), to_regclass('public.alembic_version')").fetchone()
            current, has_alembic = row if row else (None, None)
            revision = None
            if has_alembic:
                version = conn.execute("SELECT version_num FROM public.alembic_version").fetchone()
                revision = version[0] if version else None
    except psycopg.OperationalError:
        sys.stderr.write(
            f"Stopped: cannot connect to database {args.database} as the migrator. Check that the "
            "Compose services are running and that the database exists. This script never creates it.\n"
        )
        return 1
    if current != args.database:
        sys.stderr.write(f"Stopped: the migrator connected to {current}, expected {args.database}.\n")
        return 1
    sys.stdout.write(
        f"Target database {current} (Alembic revision {revision or 'none'}), anchor bucket {args.bucket}.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
