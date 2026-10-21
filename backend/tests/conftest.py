"""Shared fixtures: a real MongoDB 8 replica set with authentication, set up with the same
database users and custom roles as Atlas (scripts/mongo_roles.py), migrated with the real
migration command's code.

Each test database gets the roles for its own name, and the API, worker, retention and
migration users connect only as themselves, so a missing privilege fails a test the way it
would fail in production.
"""

import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest
from pydantic import SecretStr
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError
from testcontainers.core.container import DockerContainer

from app.metadata import MODELS
from app.platform.db import Database, create_client
from app.platform.files import FILE_INDEXES
from app.platform.schema import migrate
from migrations import MIGRATIONS
from scripts.mongo_roles import ROLE_NAMES, migrator_roles, role_privileges

if TYPE_CHECKING:
    from tests.api_support import Api

MONGO_IMAGE = "mongo:8.0"
# Never `hrms`: local and test processes refuse the production database's name.
DATABASE_NAME = "hrms_test"
USERS = ("hrms_migrator", *ROLE_NAMES)
# The image's entrypoint creates the root user; the replica set needs a key file for its
# members to authenticate each other, which the container writes before starting mongod.
START = (
    "head -c 756 /dev/urandom | base64 -w0 > /tmp/keyfile && chmod 400 /tmp/keyfile "
    "&& chown mongodb:mongodb /tmp/keyfile "
    "&& exec docker-entrypoint.sh mongod --replSet rs0 --keyFile /tmp/keyfile --bind_ip_all"
)


@dataclass(frozen=True)
class MongoServer:
    host: str
    port: int
    root_password: str
    passwords: dict[str, str]

    def uri(self, user: str) -> SecretStr:
        password = self.root_password if user == "root" else self.passwords[user]
        return SecretStr(
            f"mongodb://{user}:{quote(password)}@{self.host}:{self.port}/?authSource=admin&directConnection=true"
        )

    def admin(self) -> MongoClient[dict[str, Any]]:
        return MongoClient(self.uri("root").get_secret_value(), uuidRepresentation="standard", tz_aware=True)

    def grant(self, database: str) -> None:
        """Custom roles for `database`, granted to the users, as the Atlas set-up does."""
        with self.admin() as client:
            admin = client.admin
            for role, privileges in role_privileges(database).items():
                name = f"{role}@{database}"
                try:
                    admin.command("createRole", name, privileges=privileges, roles=[])
                except OperationFailure as exc:
                    if exc.code != 51002:  # the role already exists
                        raise
                admin.command("grantRolesToUser", role, roles=[name])
            admin.command("grantRolesToUser", "hrms_migrator", roles=migrator_roles(database))


def _wait_for_mongo(server: MongoServer, timeout_seconds: float = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            client: MongoClient[dict[str, Any]]
            with MongoClient(server.uri("root").get_secret_value(), serverSelectionTimeoutMS=1000) as client:
                client.admin.command("ping")
                return
        except PyMongoError:
            time.sleep(0.5)
    raise TimeoutError("MongoDB test container did not become ready")


def _initiate(server: MongoServer) -> None:
    with server.admin() as client:
        try:
            client.admin.command(
                "replSetInitiate", {"_id": "rs0", "members": [{"_id": 0, "host": "localhost:27017"}]}
            )
        except OperationFailure as exc:
            if exc.code != 23:  # already initialized
                raise
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if client.admin.command("hello").get("isWritablePrimary"):
                break
            time.sleep(0.3)
        for user in USERS:
            client.admin.command("createUser", user, pwd=server.passwords[user], roles=[])


@pytest.fixture(scope="session")
def mongo() -> Iterator[MongoServer]:
    root_password = secrets.token_urlsafe(24)
    container = (
        DockerContainer(MONGO_IMAGE)
        .with_env("MONGO_INITDB_ROOT_USERNAME", "root")
        .with_env("MONGO_INITDB_ROOT_PASSWORD", root_password)
        .with_exposed_ports(27017)
        .with_kwargs(entrypoint=["bash", "-c", START])
    )
    with container:
        server = MongoServer(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(27017)),
            root_password=root_password,
            passwords={user: secrets.token_urlsafe(24) for user in USERS},
        )
        _wait_for_mongo(server)
        _initiate(server)
        yield server


async def migrate_database(server: MongoServer, name: str) -> None:
    server.grant(name)
    database = Database(create_client(server.uri("hrms_migrator"), application_name="test-migrate"), name)
    try:
        await migrate(database, MODELS, MIGRATIONS, FILE_INDEXES)
    finally:
        await database.dispose()


@pytest.fixture(scope="session")
async def migrated_mongo(mongo: MongoServer) -> MongoServer:
    """The `hrms` database with every collection, validator and index."""
    await migrate_database(mongo, DATABASE_NAME)
    return mongo


async def fresh_migrated_database(server: MongoServer) -> str:
    """A database of its own, migrated, for tests whose assertions depend on everything in a
    collection."""
    name = f"hrms_scratch_{secrets.token_hex(4)}"
    await migrate_database(server, name)
    return name


def fresh_database(server: MongoServer) -> str:
    """An empty database with the users' roles granted and nothing created yet."""
    name = f"hrms_scratch_{secrets.token_hex(4)}"
    server.grant(name)
    return name


@asynccontextmanager
async def database_as(server: MongoServer, user: str, name: str = DATABASE_NAME) -> AsyncIterator[Database]:
    database = Database(create_client(server.uri(user), application_name=f"test-{user}"), name)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def api(migrated_mongo: MongoServer) -> AsyncIterator["Api"]:
    """A running API on the shared test database, for an organization of its own."""
    from tests.api_support import running_api  # noqa: PLC0415 (api_support imports this module)

    async with running_api(migrated_mongo) as running:
        yield running


@pytest.fixture
async def app_database(migrated_mongo: MongoServer) -> AsyncIterator[Database]:
    """The shared test database as the API's user, with units of work in an organization of the
    test's own (as an authenticated request's are)."""
    from tests.api_support import ScopedDatabase, create_organization  # noqa: PLC0415

    uri = migrated_mongo.uri("hrms_app")
    organization_id = await create_organization(uri, DATABASE_NAME)
    database = ScopedDatabase(uri, DATABASE_NAME, organization_id, "test-app")
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def files_api(migrated_mongo: MongoServer) -> AsyncIterator["Api"]:
    """A running API with document uploads turned on (as with ClamAV available); files are stored
    in the database's GridFS bucket and `api.files` is that bucket as the worker uses it."""
    from tests.api_support import running_api  # noqa: PLC0415 (api_support imports this module)

    async with running_api(migrated_mongo, document_uploads_enabled=True) as running:
        yield running


@pytest.fixture
async def isolated_api(migrated_mongo: MongoServer) -> AsyncIterator["Api"]:
    """A running API on a database of its own, for tests whose assertions depend on everything
    in a collection, such as which outbox messages one dispatcher run claims."""
    from tests.api_support import running_api  # noqa: PLC0415 (api_support imports this module)

    async with running_api(
        migrated_mongo, database_name=await fresh_migrated_database(migrated_mongo)
    ) as running:
        yield running
