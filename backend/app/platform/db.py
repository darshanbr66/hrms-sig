"""MongoDB access: document models, the tenant-scoped unit of work and the client (ADR-030).

Models are dataclasses that subclass `Document`. Each names its collection, its indexes and
whether it is append-only; `schema.py` turns those declarations into collections, `$jsonSchema`
validators and indexes. Services work through a `Session`, one per unit of work:

- **One transaction.** `Database.unit_of_work()` runs a multi-document transaction (snapshot
  reads, majority writes). New and changed documents are written at `flush()` and on commit;
  nothing is written when the block raises.
- **Tenant isolation.** A session belongs to one organization. Every read, write, count and
  aggregation of a tenant-owned collection carries `organization_id` added here, so a query
  cannot forget it; a tenant-owned document cannot be written or read without an organization.
  `find_any_tenant` is the one explicit exception, for resolving a sign-in, an invitation or a
  reset token to its organization.
- **Append-only collections** (audit streams, attendance events, the leave ledger, histories)
  refuse updates and deletes here; the database user of the API has no update or remove
  privilege on them either (docs/deployment.md, Atlas custom roles).
- **Locks.** `for_update=True` writes a lock counter on the document inside the transaction.
  A second transaction writing the same document then fails with a write conflict instead of
  waiting, which the API answers with `409 concurrent-change`: two concurrent changes of one
  record never both apply. Where the loser must instead see what the winner committed (a
  single-use token used twice is the theft signal; a sign-out must not fail),
  `Database.retry_on_conflict` runs its unit of work again, as a row lock's waiter would.

Values are stored with fixed encodings: UUIDs as BSON UUIDs, datetimes as UTC BSON dates
(millisecond precision; they are truncated when written), dates and times as ISO strings,
decimals as Decimal128 with two decimal places (every stored decimal is a day count or an amount,
formerly numeric(5,2); rounded half up when written, as PostgreSQL did), durations as whole
milliseconds.
"""

import asyncio
import contextvars
import dataclasses
import logging
import secrets
import types
import typing
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from ipaddress import IPv4Address, IPv6Address
from typing import Any, ClassVar, Final, TypeVar

from bson import Decimal128
from bson.binary import UuidRepresentation
from bson.codec_options import CodecOptions
from pydantic import SecretStr
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    OperationFailure,
    PyMongoError,
)
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from app.platform.ids import uuid7

logger = logging.getLogger(__name__)
# The driver's DEBUG command log prints whole commands and replies: documents with names, email
# addresses and token hashes. It never reaches the logs, whatever LOG_LEVEL says (rule 8).
logging.getLogger("pymongo").setLevel(logging.WARNING)

T = TypeVar("T", bound="Document")

ID: Final = "_id"
# What an erasable field holds once a person's data was erased (`Document.__erasable__`).
ERASED: Final = "[erased]"
LOCK_FIELD: Final = "_lk"
TENANT_FIELD: Final = "organization_id"
# Case-insensitive comparison for codes, names and email addresses (formerly `citext`).
CASE_INSENSITIVE: Final[dict[str, Any]] = {"locale": "en", "strength": 2}
# Server error codes and labels that mean "another transaction changed this document".
_WRITE_CONFLICT: Final = 112
_TRANSIENT: Final = "TransientTransactionError"
_UNKNOWN_COMMIT: Final = "UnknownTransactionCommitResult"
COMMIT_ATTEMPTS: Final = 3
# How often `retry_on_conflict` runs a unit of work that lost a write conflict, and the longest
# pause between runs. MongoDB reports the conflict at once, while the winner's transaction may
# still be open; the pauses (20, 40, 80 ... ms, at most 500, plus jitter) outlast it.
CONFLICT_ATTEMPTS: Final = 8
CONFLICT_MAX_PAUSE_MS: Final = 500
CENTS: Final = Decimal("0.01")

CODEC_OPTIONS: Final[CodecOptions[dict[str, Any]]] = CodecOptions(
    tz_aware=True, tzinfo=UTC, uuid_representation=UuidRepresentation.STANDARD
)


def utcnow() -> datetime:
    return truncate(datetime.now(UTC))


def truncate(moment: datetime) -> datetime:
    """A datetime as MongoDB stores it: UTC, millisecond precision."""
    if moment.tzinfo is None:
        raise ValueError("naive datetimes are not stored; use an aware UTC datetime")
    moment = moment.astimezone(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


# --- errors -------------------------------------------------------------------------------------


class ConcurrentChangeError(Exception):
    """Another transaction changed a document this unit of work also changed."""


class UniqueViolationError(Exception):
    """A write broke a unique index; `index` is its name (`uq_<collection>_...`)."""

    def __init__(self, index: str) -> None:
        super().__init__(f"unique index {index} rejected the write")
        self.index = index


class TenantScopeError(RuntimeError):
    """A tenant-owned collection was used without, or outside, an organization."""


class AppendOnlyError(RuntimeError):
    """An update or delete of an append-only document was attempted."""


class GuardedUpdateError(RuntimeError):
    """An update broke a model's update rules (a field that never changes, a value recorded
    once, a status transition that is not allowed); formerly a guard trigger."""


def _unique_index_name(exc: DuplicateKeyError) -> str:
    details = exc.details or {}
    message = str(details.get("errmsg", "")) or str(exc)
    marker = "index: "
    if marker in message:
        return message.split(marker, 1)[1].split(" ", 1)[0]
    return "unknown"


def _is_conflict(exc: PyMongoError) -> bool:
    if isinstance(exc, OperationFailure) and exc.code == _WRITE_CONFLICT:
        return True
    return exc.has_error_label(_TRANSIENT)


# --- models -------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Index:
    """An index of a collection. Keys use stored field names (`_id` for the identifier).

    Every index of a tenant-owned collection that should be unique per organization lists
    `organization_id` first."""

    keys: tuple[tuple[str, int], ...]
    name: str
    unique: bool = False
    partial: Mapping[str, Any] | None = None
    collation: Mapping[str, Any] | None = None
    expire_after_seconds: int | None = None

    def options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"name": self.name}
        if self.unique:
            options["unique"] = True
        if self.partial is not None:
            options["partialFilterExpression"] = dict(self.partial)
        if self.collation is not None:
            options["collation"] = dict(self.collation)
        if self.expire_after_seconds is not None:
            options["expireAfterSeconds"] = self.expire_after_seconds
        return options


def asc(*fields: str) -> tuple[tuple[str, int], ...]:
    return tuple((name, 1) for name in fields)


def allowed(values: type[Enum] | tuple[str, ...]) -> dict[str, Any]:
    """A validator rule: the field holds one of these values (an enum's, or the ones given)."""
    if isinstance(values, tuple):
        return {"enum": list(values)}
    return {"enum": [member.value for member in values]}


@dataclasses.dataclass(kw_only=True, eq=False)
class Document:
    """Base of every stored document. Subclasses are `@dataclasses.dataclass(kw_only=True,
    eq=False)` and set the class attributes below. Equality is identity, as for the records a
    unit of work hands out."""

    __collection__: ClassVar[str]
    __id_field__: ClassVar[str] = "id"
    __tenant_scoped__: ClassVar[bool] = True
    __append_only__: ClassVar[bool] = False
    __indexes__: ClassVar[tuple[Index, ...]] = ()
    # Extra `$jsonSchema` rules per field (enums, lengths, patterns), merged into the generated one.
    __field_rules__: ClassVar[Mapping[str, Mapping[str, Any]]] = {}
    # Rules across fields of one document (formerly CHECK constraints), as `$expr` conditions the
    # validator also requires, for example an end date after the start date.
    __expr_rules__: ClassVar[tuple[Mapping[str, Any], ...]] = ()
    # Update rules, enforced by the unit of work on every change (formerly guard triggers):
    # only these fields may change (None: any field); these, once set, never change again;
    # and per field, the values each value may move to.
    __deletable__: ClassVar[bool] = True
    __mutable__: ClassVar[frozenset[str] | None] = None
    __set_once__: ClassVar[frozenset[str]] = frozenset()
    __transitions__: ClassVar[Mapping[str, Mapping[str, frozenset[str]]]] = {}
    # Free-text fields that the permanent erasure of a person's data may overwrite, and only
    # with `ERASED` (docs/database-design.md §9.1); every other rule above still applies.
    __erasable__: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def check_update(cls, before: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        """Refuse a change the model's update rules forbid (stored names and values)."""
        names = {("id" if key == ID else key) for key in changed} - {LOCK_FIELD}
        names -= {name for name in names & cls.__erasable__ if changed[name] == ERASED}
        if cls.__mutable__ is not None and not names <= cls.__mutable__:
            raise GuardedUpdateError(f"{cls.__collection__}: {sorted(names - cls.__mutable__)} never change")
        for name in names & cls.__set_once__:
            if before.get(name) is not None:
                raise GuardedUpdateError(f"{cls.__collection__}: {name} is recorded once")
        for name, allowed_moves in cls.__transitions__.items():
            if name in names and changed[name] not in allowed_moves.get(str(before.get(name)), frozenset()):
                raise GuardedUpdateError(
                    f"{cls.__collection__}: {name} cannot move from {before.get(name)} to {changed[name]}"
                )

    @classmethod
    def field_types(cls) -> dict[str, Any]:
        cached = cls.__dict__.get("_field_types_cache")
        if cached is None:
            hints = typing.get_type_hints(cls)
            cached = {f.name: hints[f.name] for f in dataclasses.fields(cls)}
            cls._field_types_cache = cached  # type: ignore[attr-defined]
        return cached

    def identity(self) -> Any:
        return getattr(self, type(self).__id_field__)

    def before_write(self) -> None:
        """Called before the document is written: keeps stored derived fields (search text,
        sort keys) in step with the fields they come from."""


@dataclasses.dataclass(kw_only=True, eq=False)
class Record(Document):
    """A document with a UUIDv7 identifier owned by one organization."""

    id: uuid.UUID = dataclasses.field(default_factory=uuid7)
    organization_id: uuid.UUID | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class NamedLock(Document):
    """`named_locks`: one document per lock name (`<organization>:<name>`), written by
    `Session.lock`. It holds nothing but a counter."""

    __collection__ = "named_locks"
    __id_field__ = "key"
    __tenant_scoped__ = False

    key: str
    taken: int = 0


# --- encoding -----------------------------------------------------------------------------------


def encode(value: Any) -> Any:  # noqa: PLR0911 (one encoding per stored type)
    """A Python value as stored. Used for documents and for query values alike."""
    if value is None or isinstance(value, (bool, int, float, str, uuid.UUID, bytes)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return truncate(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return Decimal128(value.quantize(CENTS, rounding=ROUND_HALF_UP))
    if isinstance(value, timedelta):
        return int(value.total_seconds() * 1000)
    if isinstance(value, (IPv4Address, IPv6Address)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(item) for item in value]
    raise TypeError(f"cannot store a value of type {type(value).__name__}")


def _unwrap(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


def decode(annotation: Any, value: Any) -> Any:  # noqa: PLR0911 (one decoding per stored type)
    if value is None:
        return None
    target = _unwrap(annotation)
    if target is date:
        return date.fromisoformat(value)
    if target is time:
        return time.fromisoformat(value)
    if target is Decimal:
        number = value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))
        return number.quantize(CENTS, rounding=ROUND_HALF_UP)
    if target is timedelta:
        return timedelta(milliseconds=int(value))
    if target is bytes:
        return bytes(value)
    if target is datetime:
        return value
    if isinstance(target, type) and issubclass(target, Enum):
        return target(value)
    if typing.get_origin(target) is tuple and isinstance(value, list):
        return tuple(value)
    return value


def to_document(obj: Document) -> dict[str, Any]:
    cls = type(obj)
    stored: dict[str, Any] = {}
    for name in cls.field_types():
        key = ID if name == cls.__id_field__ else name
        stored[key] = encode(getattr(obj, name))
    return stored


def from_document[D: Document](cls: type[D], stored: Mapping[str, Any]) -> D:
    values: dict[str, Any] = {}
    for name, annotation in cls.field_types().items():
        key = ID if name == cls.__id_field__ else name
        if key in stored:
            values[name] = decode(annotation, stored[key])
    obj = cls(**values)
    return obj


def stored_name(cls: type[Document], name: str) -> str:
    return ID if name == cls.__id_field__ else name


def encode_filter(cls: type[Document], query: Mapping[str, Any]) -> dict[str, Any]:
    """A filter with field names as stored and values encoded as stored."""
    encoded: dict[str, Any] = {}
    for key, value in query.items():
        if key in ("$and", "$or", "$nor"):
            encoded[key] = [encode_filter(cls, part) for part in value]
        elif key.startswith("$"):
            encoded[key] = encode(value)
        else:
            encoded[stored_name(cls, key)] = _encode_condition(value)
    return encoded


def _encode_condition(value: Any) -> Any:
    if isinstance(value, Mapping) and value and all(str(key).startswith("$") for key in value):
        return {
            key: (_encode_condition(item) if key == "$not" else encode(item)) for key, item in value.items()
        }
    return encode(value)


def encode_sort(cls: type[Document], sort: Sequence[tuple[str, int]] | None) -> list[tuple[str, int]] | None:
    if not sort:
        return None
    return [(stored_name(cls, name), direction) for name, direction in sort]


# --- tenant scope -------------------------------------------------------------------------------

_current_tenant: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar("tenant", default=None)


@contextmanager
def tenant_scope(organization_id: uuid.UUID) -> Iterator[None]:
    """Units of work opened inside belong to this organization (the worker's per-organization
    jobs, operator commands and tests)."""
    token = _current_tenant.set(organization_id)
    try:
        yield
    finally:
        _current_tenant.reset(token)


def enter_tenant(organization_id: uuid.UUID) -> None:
    """Bind the rest of the current request to the authenticated actor's organization. Called
    once per request, by the dependency that authenticates it; a second, different organization
    in the same request is refused."""
    current = _current_tenant.get()
    if current is not None and current != organization_id:
        raise TenantScopeError("this request already belongs to another organization")
    _current_tenant.set(organization_id)


# Pipeline stages that read another collection (and the key naming it), and those that write.
_FOREIGN_STAGES: Final = {"$lookup": "from", "$graphLookup": "from", "$unionWith": "coll"}
_WRITE_STAGES: Final = frozenset({"$out", "$merge"})


def _tenant_collections() -> frozenset[str]:
    """The names of every tenant-owned collection among the document models defined so far."""
    names: set[str] = set()
    pending = list(Document.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__tenant_scoped__ and isinstance(getattr(cls, "__collection__", None), str):
            names.add(cls.__collection__)
    return frozenset(names)


def current_tenant() -> uuid.UUID | None:
    return _current_tenant.get()


def reset_tenant() -> contextvars.Token[uuid.UUID | None]:
    """Start a request outside any organization; pass the result to `restore_tenant` at its end."""
    return _current_tenant.set(None)


def restore_tenant(token: contextvars.Token[uuid.UUID | None]) -> None:
    _current_tenant.reset(token)


# --- the unit of work ---------------------------------------------------------------------------


class Session:
    """One unit of work in one transaction, for one organization (or none, for public flows
    until they resolve which organization a request belongs to)."""

    def __init__(
        self, database: "Database", client_session: AsyncClientSession, tenant: uuid.UUID | None
    ) -> None:
        self._database = database
        self._client_session = client_session
        self._tenant = tenant
        self._identity: dict[tuple[str, Any], Document] = {}
        self._snapshots: dict[tuple[str, Any], dict[str, Any]] = {}
        self._new: list[Document] = []
        self._new_keys: set[tuple[str, Any]] = set()
        self._commit_checks: dict[str, Callable[[Session], Awaitable[None]]] = {}

    # -- tenant ---------------------------------------------------------------------------------

    @property
    def tenant(self) -> uuid.UUID | None:
        return self._tenant

    def require_tenant(self) -> uuid.UUID:
        """The session's organization, for a collection that is not tenant-scoped but is read per
        organization (security events); refused outside one, so such a read never falls through
        to the events that belong to no organization."""
        if self._tenant is None:
            raise TenantScopeError("this read is made only inside an organization")
        return self._tenant

    def bind_tenant(self, organization_id: uuid.UUID) -> None:
        """Bind an unbound session (sign-in, invitation, reset) to the organization it resolved."""
        if self._tenant is not None and self._tenant != organization_id:
            raise TenantScopeError("this unit of work already belongs to another organization")
        self._tenant = organization_id

    def _scoped(self, cls: type[Document], query: Mapping[str, Any] | None) -> dict[str, Any]:
        encoded = encode_filter(cls, query or {})
        if not cls.__tenant_scoped__:
            return encoded
        if self._tenant is None:
            raise TenantScopeError(f"{cls.__collection__} is read only inside an organization")
        if TENANT_FIELD in encoded and encoded[TENANT_FIELD] != self._tenant:
            raise TenantScopeError("a query named another organization")
        encoded[TENANT_FIELD] = self._tenant
        return encoded

    # -- collections ----------------------------------------------------------------------------

    def collection(self, cls: type[Document]) -> AsyncCollection[dict[str, Any]]:
        return self._database.collection(cls.__collection__)

    @property
    def client_session(self) -> AsyncClientSession:
        return self._client_session

    def _key(self, cls: type[Document], identifier: Any) -> tuple[str, Any]:
        return (cls.__collection__, identifier)

    def _adopt(self, cls: type[T], stored: Mapping[str, Any]) -> T:
        """The identity-mapped instance for a stored document; a pending change already made
        to a loaded instance is kept."""
        key = self._key(cls, stored[ID])
        existing = self._identity.get(key)
        if existing is not None:
            return existing  # type: ignore[return-value]
        obj = from_document(cls, stored)
        self._identity[key] = obj
        self._snapshots[key] = to_document(obj)
        return obj

    # -- reads ----------------------------------------------------------------------------------

    async def get(self, cls: type[T], identifier: Any, *, for_update: bool = False) -> T | None:
        key = self._key(cls, identifier)
        if not for_update and key in self._identity:
            return self._identity[key]  # type: ignore[return-value]
        query = self._scoped(cls, {cls.__id_field__: identifier})
        if for_update:
            return await self._lock(cls, query)
        stored = await self.collection(cls).find_one(query, session=self._client_session)
        return None if stored is None else self._adopt(cls, stored)

    async def _lock(self, cls: type[T], query: Mapping[str, Any]) -> T | None:
        if cls.__append_only__:
            raise AppendOnlyError(f"{cls.__collection__} is append-only and is never locked for update")
        try:
            stored = await self.collection(cls).find_one_and_update(
                query,
                {"$inc": {LOCK_FIELD: 1}},
                session=self._client_session,
                return_document=ReturnDocument.AFTER,
            )
        except PyMongoError as exc:
            raise self._translate(exc) from exc
        if stored is None:
            return None
        key = self._key(cls, stored[ID])
        existing = self._identity.get(key)
        if existing is None:
            return self._adopt(cls, stored)
        # Refresh what this unit of work has not changed itself with the locked version.
        fresh = from_document(cls, stored)
        snapshot = self._snapshots.get(key, {})
        current = to_document(existing)
        for name in cls.field_types():
            stored_key = stored_name(cls, name)
            if current.get(stored_key) == snapshot.get(stored_key):
                setattr(existing, name, getattr(fresh, name))
        self._snapshots[key] = to_document(fresh)
        return existing  # type: ignore[return-value]

    async def find(
        self,
        cls: type[T],
        query: Mapping[str, Any] | None = None,
        *,
        sort: Sequence[tuple[str, int]] | None = None,
        limit: int = 0,
        skip: int = 0,
        collation: Mapping[str, Any] | None = None,
    ) -> list[T]:
        cursor = self.collection(cls).find(
            self._scoped(cls, query),
            sort=encode_sort(cls, sort),
            limit=limit,
            skip=skip,
            collation=dict(collation) if collation else None,
            session=self._client_session,
        )
        return [self._adopt(cls, stored) async for stored in cursor]

    async def find_one(
        self,
        cls: type[T],
        query: Mapping[str, Any],
        *,
        sort: Sequence[tuple[str, int]] | None = None,
        for_update: bool = False,
        collation: Mapping[str, Any] | None = None,
    ) -> T | None:
        if for_update:
            found = await self.find(cls, query, sort=sort, limit=1, collation=collation)
            if not found:
                return None
            return await self.get(cls, found[0].identity(), for_update=True)
        items = await self.find(cls, query, sort=sort, limit=1, collation=collation)
        return items[0] if items else None

    async def find_any_tenant(self, cls: type[T], query: Mapping[str, Any]) -> T | None:
        """One document found in any organization. Only for resolving the organization of an
        unauthenticated request (a sign-in email, an invitation or reset token, a session
        token); the caller binds the session to the result's organization."""
        stored = await self.collection(cls).find_one(encode_filter(cls, query), session=self._client_session)
        return None if stored is None else self._adopt(cls, stored)

    async def exists_any_tenant(self, cls: type[Document], query: Mapping[str, Any]) -> bool:
        """Whether a matching document exists in any organization, without loading it (an email
        address is unique across organizations)."""
        stored = await self.collection(cls).find_one(
            encode_filter(cls, query), projection={ID: 1}, session=self._client_session
        )
        return stored is not None

    async def count(self, cls: type[Document], query: Mapping[str, Any] | None = None) -> int:
        return await self.collection(cls).count_documents(
            self._scoped(cls, query), session=self._client_session
        )

    async def exists(self, cls: type[Document], query: Mapping[str, Any]) -> bool:
        stored = await self.collection(cls).find_one(
            self._scoped(cls, query), projection={ID: 1}, session=self._client_session
        )
        return stored is not None

    async def distinct(
        self, cls: type[Document], name: str, query: Mapping[str, Any] | None = None
    ) -> list[Any]:
        values = await self.collection(cls).distinct(
            stored_name(cls, name), self._scoped(cls, query), session=self._client_session
        )
        annotation = cls.field_types().get(name)
        return [decode(annotation, value) for value in values] if annotation is not None else list(values)

    async def aggregate(
        self, cls: type[Document], pipeline: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Raw stored documents from a pipeline; tenant-owned collections are matched to the
        session's organization first. Lookups into other collections add their own
        `organization_id` condition (see `tenant_lookup`): a stage that reads another tenant-owned
        collection without it, or one that writes (`$out`, `$merge`), is refused."""
        self._check_pipeline(pipeline, _tenant_collections())
        stages: list[Mapping[str, Any]] = []
        if cls.__tenant_scoped__:
            stages.append({"$match": self._scoped(cls, {})})
        stages.extend(pipeline)
        cursor = await self.collection(cls).aggregate(list(stages), session=self._client_session)
        return [item async for item in cursor]

    def _check_pipeline(self, pipeline: Sequence[Mapping[str, Any]], tenant_owned: frozenset[str]) -> None:
        for stage in pipeline:
            for name, spec in stage.items():
                if name in _WRITE_STAGES:
                    raise TenantScopeError(f"{name} is not allowed in a pipeline")
                if name == "$facet":
                    for branch in spec.values():
                        self._check_pipeline(branch, tenant_owned)
                    continue
                if name not in _FOREIGN_STAGES:
                    continue
                target = spec if isinstance(spec, str) else spec.get(_FOREIGN_STAGES[name])
                nested = [] if isinstance(spec, str) else spec.get("pipeline", [])
                if target in tenant_owned and not (name == "$lookup" and self._own_organization_only(nested)):
                    raise TenantScopeError(
                        f"a {name} into {target} must be restricted to the organization (tenant_lookup)"
                    )
                self._check_pipeline(nested, tenant_owned)

    def _own_organization_only(self, pipeline: Sequence[Mapping[str, Any]]) -> bool:
        """Whether a lookup's pipeline starts by matching this session's organization."""
        if self._tenant is None or not pipeline:
            return False
        first = pipeline[0].get("$match")
        return isinstance(first, Mapping) and first.get(TENANT_FIELD) == self._tenant

    def tenant_lookup(self, other: type[Document], *, local: str, foreign: str, into: str) -> dict[str, Any]:
        """A `$lookup` stage into another tenant-owned collection, restricted to this session's
        organization."""
        match: dict[str, Any] = {"$expr": {"$eq": [f"${foreign}", "$$key"]}}
        if other.__tenant_scoped__:
            match[TENANT_FIELD] = self._scoped(other, {})[TENANT_FIELD]
        return {
            "$lookup": {
                "from": other.__collection__,
                "let": {"key": f"${local}"},
                "pipeline": [{"$match": match}],
                "as": into,
            }
        }

    # -- writes ---------------------------------------------------------------------------------

    def add(self, obj: Document) -> None:
        cls = type(obj)
        if cls.__tenant_scoped__:
            if self._tenant is None:
                raise TenantScopeError(f"{cls.__collection__} is written only inside an organization")
            current = getattr(obj, TENANT_FIELD, None)
            if current is None:
                setattr(obj, TENANT_FIELD, self._tenant)
            elif current != self._tenant:
                raise TenantScopeError("a document of another organization was added")
        key = self._key(cls, obj.identity())
        if key in self._identity:
            if self._identity[key] is not obj:
                raise UniqueViolationError(f"pk_{cls.__collection__}")
            return
        self._identity[key] = obj
        self._new.append(obj)
        self._new_keys.add(key)

    async def insert_if_absent(self, obj: Document, keys: Sequence[str]) -> bool:
        """Insert `obj` unless a document with the same values of `keys` exists (an idempotency
        key); True when it was inserted. One atomic upsert, so concurrent requests with the same
        key insert one document, and a duplicate never aborts the transaction. For an append-only
        collection, a check and an insert under the caller's lock."""
        cls = type(obj)
        if cls.__tenant_scoped__:
            if self._tenant is None:
                raise TenantScopeError(f"{cls.__collection__} is written only inside an organization")
            if getattr(obj, TENANT_FIELD, None) is None:
                setattr(obj, TENANT_FIELD, self._tenant)
        await self.flush()
        obj.before_write()
        stored = to_document(obj)
        query = {stored_name(cls, key): stored[stored_name(cls, key)] for key in keys}
        if cls.__tenant_scoped__:
            query[TENANT_FIELD] = self._tenant
        inserted = {name: value for name, value in stored.items() if name not in query}
        if cls.__append_only__:
            # The database user may only insert here (no upsert): a plain check and insert. The
            # caller holds the lock that serializes writers of this key; the unique index is the
            # backstop.
            if await self.collection(cls).find_one(query, projection={ID: 1}, session=self._client_session):
                return False
            self.add(obj)
            await self.flush()
            return True
        try:
            before = await self.collection(cls).find_one_and_update(
                query,
                {"$setOnInsert": inserted},
                upsert=True,
                return_document=ReturnDocument.BEFORE,
                session=self._client_session,
            )
        except PyMongoError as exc:
            raise self._translate(exc) from exc
        if before is not None:
            return False
        key = self._key(cls, obj.identity())
        self._identity[key] = obj
        self._snapshots[key] = stored
        return True

    def add_all(self, objs: Sequence[Document]) -> None:
        for obj in objs:
            self.add(obj)

    async def flush(self) -> None:
        """Write new documents, then changed ones."""
        try:
            pending, self._new = self._new, []
            for obj in pending:
                obj.before_write()
                stored = to_document(obj)
                await self.collection(type(obj)).insert_one(stored, session=self._client_session)
                key = self._key(type(obj), obj.identity())
                self._snapshots[key] = stored
                self._new_keys.discard(key)
            for key, obj in list(self._identity.items()):
                if key in self._new_keys:
                    continue
                snapshot = self._snapshots.get(key)
                if snapshot is None:
                    continue
                obj.before_write()
                current = to_document(obj)
                changed = {name: value for name, value in current.items() if snapshot.get(name) != value}
                if not changed:
                    continue
                cls = type(obj)
                if cls.__append_only__:
                    raise AppendOnlyError(f"{cls.__collection__} is append-only; documents are never changed")
                if ID in changed or TENANT_FIELD in changed:
                    raise TenantScopeError("a document's identifier and organization never change")
                cls.check_update(snapshot, changed)
                query: dict[str, Any] = {ID: snapshot[ID]}
                if cls.__tenant_scoped__:
                    query[TENANT_FIELD] = snapshot[TENANT_FIELD]
                result = await self.collection(cls).update_one(
                    query, {"$set": changed}, session=self._client_session
                )
                if result.matched_count != 1:
                    raise ConcurrentChangeError(f"{cls.__collection__} document disappeared")
                self._snapshots[key] = current
        except PyMongoError as exc:
            raise self._translate(exc) from exc

    async def delete(self, obj: Document) -> None:
        cls = type(obj)
        if cls.__append_only__ or not cls.__deletable__:
            raise AppendOnlyError(f"{cls.__collection__} is append-only; documents are never deleted")
        key = self._key(cls, obj.identity())
        if key in self._new_keys:
            self._new = [item for item in self._new if item is not obj]
            self._new_keys.discard(key)
            self._identity.pop(key, None)
            return
        try:
            await self.collection(cls).delete_one(
                self._scoped(cls, {cls.__id_field__: obj.identity()}), session=self._client_session
            )
        except PyMongoError as exc:
            raise self._translate(exc) from exc
        self._identity.pop(key, None)
        self._snapshots.pop(key, None)

    async def refresh(self, obj: Document) -> None:
        cls = type(obj)
        stored = await self.collection(cls).find_one(
            self._scoped(cls, {cls.__id_field__: obj.identity()}), session=self._client_session
        )
        if stored is None:
            return
        fresh = from_document(cls, stored)
        for name in cls.field_types():
            setattr(obj, name, getattr(fresh, name))
        self._snapshots[self._key(cls, obj.identity())] = to_document(fresh)

    async def update_many(self, cls: type[Document], query: Mapping[str, Any], update: "Update") -> int:
        """A bulk change (for example closing every open token of a user). Instances this unit of
        work already loaded are refreshed from the database afterwards."""
        if cls.__append_only__:
            raise AppendOnlyError(f"{cls.__collection__} is append-only; documents are never changed")
        await self.flush()
        try:
            result = await self.collection(cls).update_many(
                self._scoped(cls, query), _encode_update(update), session=self._client_session
            )
        except PyMongoError as exc:
            raise self._translate(exc) from exc
        await self._reload_loaded(cls)
        return int(result.modified_count)

    async def _reload_loaded(self, cls: type[Document], only: Any = None) -> None:
        """Refresh loaded instances of `cls` (or the one with identifier `only`) from the
        database. Called after a flush, so they carry no unwritten change."""
        keys = [
            key
            for key in self._identity
            if key[0] == cls.__collection__ and key not in self._new_keys and (only is None or key[1] == only)
        ]
        if not keys:
            return
        cursor = self.collection(cls).find(
            {ID: {"$in": [key[1] for key in keys]}}, session=self._client_session
        )
        async for stored in cursor:
            key = self._key(cls, stored[ID])
            obj = self._identity[key]
            fresh = from_document(cls, stored)
            for name in cls.field_types():
                setattr(obj, name, getattr(fresh, name))
            self._snapshots[key] = to_document(fresh)

    async def find_one_and_update(
        self,
        cls: type[T],
        query: Mapping[str, Any],
        update: "Update",
        *,
        sort: Sequence[tuple[str, int]] | None = None,
        upsert: bool = False,
    ) -> T | None:
        """An atomic read-and-change (claiming a job or an outbox message, a counter)."""
        if cls.__append_only__:
            raise AppendOnlyError(f"{cls.__collection__} is append-only; documents are never changed")
        await self.flush()
        try:
            stored = await self.collection(cls).find_one_and_update(
                self._scoped(cls, query),
                _encode_update(update),
                sort=encode_sort(cls, sort),
                upsert=upsert,
                return_document=ReturnDocument.AFTER,
                session=self._client_session,
            )
        except PyMongoError as exc:
            raise self._translate(exc) from exc
        if stored is None:
            return None
        key = self._key(cls, stored[ID])
        if key in self._identity:
            await self._reload_loaded(cls, stored[ID])
            return self._identity[key]  # type: ignore[return-value]
        return self._adopt(cls, stored)

    async def delete_many(self, cls: type[Document], query: Mapping[str, Any]) -> int:
        if cls.__append_only__ or not cls.__deletable__:
            raise AppendOnlyError(f"{cls.__collection__} is append-only; documents are never deleted")
        await self.flush()
        try:
            result = await self.collection(cls).delete_many(
                self._scoped(cls, query), session=self._client_session
            )
        except PyMongoError as exc:
            raise self._translate(exc) from exc
        return int(result.deleted_count)

    async def lock(self, name: str) -> None:
        """Take a named lock of this organization for the rest of the transaction (formerly an
        advisory lock): a concurrent transaction taking the same name meets a write conflict, so
        the two never both commit. For rules no single document can carry, such as "no two
        overlapping ranges" or "one writer per employee"."""
        key = f"{self._tenant or 'platform'}:{name}"
        try:
            await self.collection(NamedLock).update_one(
                {ID: key}, {"$inc": {"taken": 1}}, upsert=True, session=self._client_session
            )
        except PyMongoError as exc:
            raise self._translate(exc) from exc

    def in_transaction(self) -> bool:
        return self._client_session.in_transaction

    def check_before_commit(self, key: str, check: Callable[["Session"], Awaitable[None]]) -> None:
        """Run `check` after the last write of this unit of work and before it commits (once per
        key). A check that raises aborts the transaction: rules across documents that the
        database cannot express (formerly deferred constraint triggers) hold at every commit."""
        self._commit_checks.setdefault(key, check)

    async def run_commit_checks(self) -> None:
        while self._commit_checks:
            _, check = self._commit_checks.popitem()
            await check(self)
            await self.flush()

    @staticmethod
    def _translate(exc: PyMongoError) -> Exception:
        if isinstance(exc, DuplicateKeyError):
            return UniqueViolationError(_unique_index_name(exc))
        if _is_conflict(exc):
            return ConcurrentChangeError(str(exc.__class__.__name__))
        return exc


def translate_error(exc: PyMongoError) -> Exception:
    """A driver error as the unit of work reports it (`ConcurrentChangeError` for a write
    conflict, `UniqueViolationError` for a duplicate key), for writes made on raw collections."""
    return Session._translate(exc)


type Update = Mapping[str, Any] | Sequence[Mapping[str, Any]]


def _encode_update(update: Update) -> dict[str, Any] | list[dict[str, Any]]:
    """An update document, or an aggregation pipeline (a list of stages), with values encoded."""
    if isinstance(update, Mapping):
        return {operator: encode(values) for operator, values in update.items()}
    return [{stage: encode(values) for stage, values in item.items()} for item in update]


# --- the database -------------------------------------------------------------------------------


def create_client(uri: SecretStr, *, application_name: str) -> AsyncMongoClient[dict[str, Any]]:
    return AsyncMongoClient(
        uri.get_secret_value(),
        appname=application_name,
        tz_aware=True,
        tzinfo=UTC,
        uuidRepresentation="standard",
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        retryWrites=True,
    )


class Database:
    def __init__(self, client: AsyncMongoClient[dict[str, Any]], name: str) -> None:
        self.client = client
        self.name = name
        self.db: AsyncDatabase[dict[str, Any]] = client.get_database(
            name,
            codec_options=CODEC_OPTIONS,
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern("majority"),
            read_preference=ReadPreference.PRIMARY,
        )

    def collection(self, name: str) -> AsyncCollection[dict[str, Any]]:
        return self.db.get_collection(name)

    @asynccontextmanager
    async def unit_of_work(self, *, tenant: uuid.UUID | None = None) -> AsyncIterator[Session]:
        """One transaction: commits when the block succeeds, aborts when it raises. Belongs to
        `tenant`, or to the organization of the current scope (`tenant_scope`, `enter_tenant`)."""
        organization = tenant if tenant is not None else _current_tenant.get()
        async with self.client.start_session(causal_consistency=True) as client_session:
            await client_session.start_transaction(
                read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority")
            )
            session = Session(self, client_session, organization)
            try:
                yield session
                await session.flush()
                await session.run_commit_checks()
            except BaseException:
                if client_session.in_transaction:
                    try:
                        await client_session.abort_transaction()
                    except PyMongoError:
                        logger.warning("db.abort_failed")
                raise
            await _commit(client_session)

    async def retry_on_conflict[R](
        self, attempt: Callable[[], Awaitable[R]], *, attempts: int = CONFLICT_ATTEMPTS
    ) -> R:
        """Run `attempt`, which opens its own unit of work, again when it loses a write conflict
        to a concurrent transaction: the next run reads what the winner committed. After
        `attempts` runs the conflict is raised (`409 concurrent-change`)."""
        for number in range(1, attempts + 1):
            try:
                return await attempt()
            except ConcurrentChangeError:
                if number == attempts:
                    raise
                # A growing pause with jitter, so the runs that collided do not collide again.
                pause = min(10 * 2**number, CONFLICT_MAX_PAUSE_MS)
                await asyncio.sleep((pause + secrets.randbelow(pause)) / 1000)
        raise AssertionError("unreachable")

    async def ping(self) -> None:
        await self.db.command("ping")

    async def dispose(self) -> None:
        await self.client.close()


async def _commit(client_session: AsyncClientSession) -> None:
    for attempt in range(1, COMMIT_ATTEMPTS + 1):
        try:
            await client_session.commit_transaction()
            return
        except PyMongoError as exc:
            if exc.has_error_label(_UNKNOWN_COMMIT) and attempt < COMMIT_ATTEMPTS:
                continue
            if _is_conflict(exc):
                raise ConcurrentChangeError("commit") from exc
            raise


def is_unavailable(exc: BaseException) -> bool:
    """The database could not be reached (readiness, fail-closed decisions)."""
    return isinstance(exc, ConnectionFailure)
