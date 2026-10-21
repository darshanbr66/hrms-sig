"""The MongoDB database users and their custom roles (docs/deployment.md §4, ADR-030).

Each process connects as its own user, holding only what that process needs:

- `hrms_app` (the API and operator commands): read and write business collections; only insert
  into append-only ones (audit streams, attendance events, the leave ledger, histories); nothing
  on the audit chain, migrations or job leases.
- `hrms_worker` (the worker): what the API can, plus write the audit chain and checkpoints and
  job leases, and remove files.
- `hrms_audit_retention` (the worker's retention job): read and remove audit records and links,
  add checkpoints; nothing else.
- the migration user (`python -m app.cli migrate`): the built-in `readWrite` and `dbAdmin` on the
  database.

Nobody but the retention user may remove or update an audit record, so even a compromised API
cannot rewrite the audit trail through its own database connection.

`python -m scripts.mongo_roles --database hrms` prints the roles as JSON for the Atlas
Administration API or the Atlas UI ("Database Access" > "Custom Roles"), so the roles in
production are exactly the ones the tests use.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, Final

from app.metadata import MODELS
from app.platform.audit.tables import (
    AuditLock,
    AuditLogRecord,
    ChainCheckpoint,
    ChainLink,
    SecurityEventRecord,
)
from app.platform.db import Document
from app.platform.files import BUCKET
from app.platform.jobs import LEASES_COLLECTION

ROLE_NAMES: Final = ("hrms_app", "hrms_worker", "hrms_audit_retention")
READ: Final = ("find",)
READ_ADD: Final = ("find", "insert")
READ_WRITE: Final = ("find", "insert", "update")
FULL: Final = ("find", "insert", "update", "remove")
FILES: Final = (f"{BUCKET}.files", f"{BUCKET}.chunks")
# Written only by the worker's integrity jobs (and the retention job).
CHAIN: Final = frozenset({ChainLink.__collection__, ChainCheckpoint.__collection__, AuditLock.__collection__})


def _actions(model: type[Document]) -> tuple[str, ...]:
    if model.__append_only__:
        return READ_ADD
    if not model.__deletable__:
        return READ_WRITE
    return FULL


def _privileges(database: str, grants: dict[str, Sequence[str]]) -> list[dict[str, Any]]:
    return [
        {"resource": {"db": database, "collection": collection}, "actions": sorted(set(actions))}
        for collection, actions in sorted(grants.items())
    ]


def role_privileges(
    database: str, models: Sequence[type[Document]] = MODELS
) -> dict[str, list[dict[str, Any]]]:
    app: dict[str, Sequence[str]] = {
        model.__collection__: _actions(model) for model in models if model.__collection__ not in CHAIN
    }
    # Files are stored and read by the API; removing them is the worker's (expired exports).
    # GridFS lists its indexes before writing; the migration command created them.
    app |= dict.fromkeys(FILES, (*READ_ADD, "listIndexes"))
    worker = dict(app)
    worker |= {model.__collection__: _actions(model) for model in models if model.__collection__ in CHAIN}
    worker |= {LEASES_COLLECTION: READ_WRITE}
    worker |= dict.fromkeys(FILES, (*FULL, "listIndexes"))
    retention: dict[str, Sequence[str]] = {
        AuditLogRecord.__collection__: ("find", "remove"),
        SecurityEventRecord.__collection__: ("find", "remove"),
        ChainLink.__collection__: ("find", "remove"),
        ChainCheckpoint.__collection__: READ_ADD,
        AuditLock.__collection__: READ_WRITE,
    }
    return {
        "hrms_app": _privileges(database, app),
        "hrms_worker": _privileges(database, worker),
        "hrms_audit_retention": _privileges(database, retention),
    }


def migrator_roles(database: str) -> list[dict[str, str]]:
    return [{"role": "readWrite", "db": database}, {"role": "dbAdmin", "db": database}]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.mongo_roles")
    parser.add_argument("--database", default="hrms")
    arguments = parser.parse_args(argv)
    document = {
        "customRoles": [
            {"roleName": name, "privileges": privileges, "roles": []}
            for name, privileges in role_privileges(arguments.database).items()
        ],
        "migratorBuiltInRoles": migrator_roles(arguments.database),
    }
    sys.stdout.write(json.dumps(document, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
