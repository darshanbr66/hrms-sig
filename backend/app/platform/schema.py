"""Collections, validators, indexes and versioned migrations for MongoDB (docs/database-design.md §11).

`python -m app.cli migrate` (as the migration database user) applies, in order:

1. **Declarations.** For every model in `app.metadata.MODELS`: the collection is created if it is
   missing, its `$jsonSchema` validator (generated from the model's fields, plus its
   `__field_rules__`) is set with `validationLevel: strict`, and every declared index is created.
   Before a validator is applied, existing documents are checked against it; any that do not
   match stop the run with their collection named (nothing is changed). An index that exists
   with the same name but another definition stops the run too. Nothing is ever dropped:
   indexes and collections that are no longer declared are only reported.
2. **Versioned data migrations** (`migrations.MIGRATIONS`) whose number is above the highest one
   recorded in `schema_migrations`, each recorded once it succeeds. Each must be idempotent.

Runs serialize on a lease in `schema_migrations` (`_id: "lock"`), so two operators cannot run
migrations at the same time.
"""

import dataclasses
import logging
import types
import typing
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Final

from pymongo.errors import OperationFailure

from app.platform.db import ID, LOCK_FIELD, Database, Document, Index, utcnow

logger = logging.getLogger(__name__)

MIGRATIONS_COLLECTION: Final = "schema_migrations"
LOCK_ID: Final = "lock"
LOCK_LEASE: Final = timedelta(minutes=15)
_DATE_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}$"
_TIME_PATTERN: Final = r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$"


class SchemaError(RuntimeError):
    """The database cannot be brought to the declared schema without changing data."""


# --- validators ---------------------------------------------------------------------------------


def _bson_rule(annotation: Any) -> dict[str, Any]:  # noqa: PLR0911, PLR0912 (one rule per stored type)
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(annotation)
        nullable = type(None) in args
        rest = [arg for arg in args if arg is not type(None)]
        rule = _bson_rule(rest[0]) if len(rest) == 1 else {}
        if nullable and "bsonType" in rule:
            kinds = rule["bsonType"] if isinstance(rule["bsonType"], list) else [rule["bsonType"]]
            rule["bsonType"] = [*kinds, "null"]
            if "enum" in rule:
                rule["enum"] = [*rule["enum"], None]
        return rule
    if annotation is uuid.UUID:
        return {"bsonType": "binData"}
    if annotation is bool:
        return {"bsonType": "bool"}
    if annotation is int or annotation is timedelta:
        return {"bsonType": ["int", "long"]}
    if annotation is float:
        return {"bsonType": ["double", "int", "long"]}
    if annotation is str:
        return {"bsonType": "string"}
    if annotation is datetime:
        return {"bsonType": "date"}
    if annotation is date:
        return {"bsonType": "string", "pattern": _DATE_PATTERN}
    if annotation is time:
        return {"bsonType": "string", "pattern": _TIME_PATTERN}
    if annotation is Decimal:
        return {"bsonType": "decimal"}
    if annotation is bytes:
        return {"bsonType": "binData"}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"bsonType": "string", "enum": [member.value for member in annotation]}
    if origin in (dict, Mapping) or annotation is dict:
        return {"bsonType": "object"}
    if origin in (list, tuple, Sequence) or annotation in (list, tuple):
        return {"bsonType": "array"}
    return {}


def validator(cls: type[Document]) -> dict[str, Any]:
    """The validator of a model: every field present with its type, nothing else stored, and its
    rules across fields."""
    properties: dict[str, Any] = {LOCK_FIELD: {"bsonType": ["int", "long"]}}
    required: list[str] = []
    for name, annotation in cls.field_types().items():
        key = ID if name == cls.__id_field__ else name
        rule = _bson_rule(annotation)
        rule.update(cls.__field_rules__.get(name, {}))
        kinds = rule.get("bsonType")
        nullable = kinds == "null" or (isinstance(kinds, list) and "null" in kinds)
        if nullable and "enum" in rule and None not in rule["enum"]:
            rule["enum"] = [*rule["enum"], None]
        properties[key] = rule
        required.append(key)
    schema = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": required,
            "properties": properties,
            "additionalProperties": False,
        }
    }
    if not cls.__expr_rules__:
        return schema
    return {"$and": [schema, *({"$expr": rule} for rule in cls.__expr_rules__)]}


# --- applying declarations ----------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class Report:
    created_collections: list[str] = dataclasses.field(default_factory=list)
    created_indexes: list[str] = dataclasses.field(default_factory=list)
    undeclared_indexes: list[str] = dataclasses.field(default_factory=list)
    undeclared_collections: list[str] = dataclasses.field(default_factory=list)
    applied_versions: list[int] = dataclasses.field(default_factory=list)


def _index_matches(
    existing: Mapping[str, Any], wanted: Mapping[str, Any], keys: Sequence[tuple[str, int]]
) -> bool:
    if list(existing.get("key", {}).items()) != list(keys):
        return False
    for option in ("unique", "partialFilterExpression", "expireAfterSeconds"):
        if existing.get(option) != wanted.get(option):
            return False
    if "collation" in wanted:
        current = existing.get("collation") or {}
        return all(current.get(name) == value for name, value in wanted["collation"].items())
    return "collation" not in existing


async def _ensure_indexes(
    database: Database, name: str, indexes: Sequence[Index], report: Report
) -> dict[str, Any]:
    existing = {item["name"]: item async for item in await database.collection(name).list_indexes()}
    for index in indexes:
        options = index.options()
        current = existing.get(index.name)
        if current is not None:
            if not _index_matches(current, options, index.keys):
                raise SchemaError(f"index {index.name} on {name} exists with another definition")
            continue
        try:
            await database.collection(name).create_index(list(index.keys), **options)
        except OperationFailure as exc:
            raise SchemaError(f"index {index.name} on {name} could not be created: {exc.code}") from exc
        report.created_indexes.append(index.name)
    return existing


async def ensure_declarations(
    database: Database,
    models: Sequence[type[Document]],
    report: Report,
    extra_indexes: Mapping[str, Sequence[Index]] | None = None,
) -> None:
    names = set(await database.db.list_collection_names())
    declared: set[str] = {MIGRATIONS_COLLECTION}
    for cls in models:
        name = cls.__collection__
        declared.add(name)
        rules = validator(cls)
        if name not in names:
            await database.db.create_collection(
                name, validator=rules, validationLevel="strict", validationAction="error"
            )
            report.created_collections.append(name)
        else:
            invalid = await database.collection(name).count_documents({"$nor": [rules]})
            if invalid:
                raise SchemaError(
                    f"{invalid} document(s) in {name} do not match the declared schema; fix the data first"
                )
            await database.db.command(
                {
                    "collMod": name,
                    "validator": rules,
                    "validationLevel": "strict",
                    "validationAction": "error",
                }
            )
        existing = await _ensure_indexes(database, name, cls.__indexes__, report)
        wanted = {index.name for index in cls.__indexes__} | {"_id_"}
        report.undeclared_indexes.extend(f"{name}.{item}" for item in existing if item not in wanted)
    for name, indexes in (extra_indexes or {}).items():
        declared.add(name)
        await _ensure_indexes(database, name, indexes, report)
    report.undeclared_collections.extend(
        sorted(name for name in names - declared if not name.startswith("system."))
    )


# --- versioned migrations -----------------------------------------------------------------------

type MigrationStep = Callable[[Database], Awaitable[None]]


@dataclasses.dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    apply: MigrationStep


async def applied_versions(database: Database) -> list[int]:
    cursor = database.collection(MIGRATIONS_COLLECTION).find({"version": {"$exists": True}})
    return sorted([int(item["version"]) async for item in cursor])


async def _acquire(database: Database, owner: str) -> None:
    now = utcnow()
    collection = database.collection(MIGRATIONS_COLLECTION)
    await collection.update_one({ID: LOCK_ID}, {"$setOnInsert": {"owner": None, "until": now}}, upsert=True)
    taken = await collection.find_one_and_update(
        {ID: LOCK_ID, "$or": [{"owner": None}, {"until": {"$lt": now}}]},
        {"$set": {"owner": owner, "until": now + LOCK_LEASE}},
    )
    if taken is None:
        raise SchemaError("another migration run holds the lock; wait for it to finish")


async def _release(database: Database, owner: str) -> None:
    await database.collection(MIGRATIONS_COLLECTION).update_one(
        {ID: LOCK_ID, "owner": owner}, {"$set": {"owner": None, "until": utcnow()}}
    )


async def migrate(
    database: Database,
    models: Sequence[type[Document]],
    migrations: Sequence[Migration],
    extra_indexes: Mapping[str, Sequence[Index]] | None = None,
) -> Report:
    versions = [item.version for item in migrations]
    if versions != sorted(set(versions)):
        raise SchemaError("migration versions must be unique and in order")
    owner = uuid.uuid4().hex
    report = Report()
    await _acquire(database, owner)
    try:
        await ensure_declarations(database, models, report, extra_indexes)
        done = set(await applied_versions(database))
        for item in migrations:
            if item.version in done:
                continue
            await item.apply(database)
            await database.collection(MIGRATIONS_COLLECTION).insert_one(
                {
                    ID: f"v{item.version:04d}",
                    "version": item.version,
                    "name": item.name,
                    "applied_at": utcnow(),
                }
            )
            report.applied_versions.append(item.version)
            logger.info("schema.migration_applied", extra={"version": item.version, "migration": item.name})
    finally:
        await _release(database, owner)
    return report


async def status(database: Database, migrations: Sequence[Migration]) -> tuple[list[int], list[int]]:
    """Applied versions and the versions still to apply."""
    done = await applied_versions(database)
    return done, [item.version for item in migrations if item.version not in set(done)]
