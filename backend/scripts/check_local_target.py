"""Check that the local processes will use the intended local MongoDB database (local only).

    uv run --env-file ../.env python -m scripts.check_local_target --database NAME [--allow-remote]

`run-backend.ps1` runs this before migrating. It stops (exit code 1) unless:

- `APP_ENV` is `local` or `test`;
- `MONGODB_DATABASE` is `NAME`, and `NAME` is not the production database's name (`hrms`);
- `MONGODB_URI` names a server on this machine, unless `--allow-remote` is given (a database of
  its own on a remote cluster, never the production one);
- the server answers as a replica set primary: the application's units of work are
  multi-document transactions, which a standalone server refuses.

It only reads the server's `hello` reply. It prints the database name, whether the server is on
this machine and the replica set's name, never a URI or credential.
"""

import argparse
import os
import sys
from typing import Any
from urllib.parse import urlsplit

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.platform.config import PRODUCTION_DATABASE

LOCAL_ENVIRONMENTS = ("local", "test")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _hosts(uri: str) -> list[str]:
    netloc = urlsplit(uri).netloc.rpartition("@")[2]
    hosts = []
    for item in netloc.split(","):
        # host:port, or [IPv6]:port
        host = item[1:].partition("]")[0] if item.startswith("[") else item.partition(":")[0]
        hosts.append(host.lower())
    return hosts


def problems(environ: dict[str, str], database: str, *, allow_remote: bool) -> list[str]:
    found = []
    if environ.get("APP_ENV") not in LOCAL_ENVIRONMENTS:
        found.append("APP_ENV must be local or test.")
    if database.lower() == PRODUCTION_DATABASE:
        found.append(
            f"{PRODUCTION_DATABASE} is the production database's name; use another, such as hrms_local."
        )
    if environ.get("MONGODB_DATABASE") != database:
        found.append(f"MONGODB_DATABASE is not {database}.")
    uri = environ.get("MONGODB_URI", "")
    if urlsplit(uri).scheme not in ("mongodb", "mongodb+srv"):
        found.append("MONGODB_URI is not set to a mongodb:// or mongodb+srv:// URI.")
    elif not allow_remote and not all(host in LOCAL_HOSTS for host in _hosts(uri)):
        found.append("MONGODB_URI does not point at a server on this machine (add --allow-remote to accept).")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.check_local_target")
    parser.add_argument("--database", required=True)
    parser.add_argument("--allow-remote", action="store_true")
    arguments = parser.parse_args(argv)
    environ = dict(os.environ)
    found = problems(environ, arguments.database, allow_remote=arguments.allow_remote)
    if found:
        sys.stderr.write("Refused:\n" + "".join(f"  {item}\n" for item in found))
        return 1
    try:
        client: MongoClient[dict[str, Any]]
        with MongoClient(environ["MONGODB_URI"], serverSelectionTimeoutMS=8000) as client:
            hello = client.admin.command("hello")
    except PyMongoError as exc:
        sys.stderr.write(f"Refused: the MongoDB server did not answer ({type(exc).__name__}).\n")
        return 1
    if not hello.get("setName") or not hello.get("isWritablePrimary"):
        sys.stderr.write("Refused: the server is not a replica set primary; transactions need one.\n")
        return 1
    where = "this machine" if not arguments.allow_remote else "remote server allowed"
    sys.stdout.write(f"Target database {arguments.database} ({where}, replica set {hello['setName']})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
