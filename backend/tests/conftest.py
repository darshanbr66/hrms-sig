"""Shared fixtures: real PostgreSQL 18 and Redis containers, set up with the same bootstrap
SQL and ACL files as the local Compose environment (infra/)."""

import asyncio
import secrets
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from botocore.config import Config
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.platform.config import AppEnv, MigrationSettings
from app.platform.db import Database, create_engine
from app.platform.storage import S3AnchorStore

if TYPE_CHECKING:
    from tests.api_support import Api

BACKEND_DIR = Path(__file__).resolve().parents[1]
INFRA_DIR = BACKEND_DIR.parent / "infra"

POSTGRES_IMAGE = "postgres:18.6"
REDIS_IMAGE = "redis:7.2.16"
# The image infra/compose.yaml uses for the audit anchor bucket.
RUSTFS_IMAGE = "rustfs/rustfs:1.0.0@sha256:8cc9801755448b71a786705ce76692c77e14936cccd87cf2fc31842e58f4d1ff"

DATABASE_NAME = "hrms"
ROLE_NAMES = ("hrms_migrator", "hrms_app", "hrms_worker", "hrms_audit_retention")


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    # psycopg's async driver needs a selector event loop, which is not the Windows default.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@dataclass(frozen=True)
class PostgresServer:
    host: str
    port: int
    superuser_password: str
    role_passwords: dict[str, str]

    def url(self, role: str, database: str = DATABASE_NAME) -> SecretStr:
        password = self.superuser_password if role == "postgres" else self.role_passwords[role]
        return SecretStr(f"postgresql://{role}:{password}@{self.host}:{self.port}/{database}")

    def connect(self, role: str, database: str = DATABASE_NAME) -> psycopg.Connection:
        return psycopg.connect(self.url(role, database).get_secret_value(), autocommit=True)

    def create_database(self, name: str) -> None:
        """A fresh database owned by the migrator, as bootstrap-roles.sql creates it."""
        with self.connect("postgres", "postgres") as connection:
            connection.execute(f'CREATE DATABASE "{name}" OWNER hrms_migrator')
            connection.execute(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
            connection.execute(
                f'GRANT CONNECT ON DATABASE "{name}" TO hrms_app, hrms_worker, hrms_audit_retention'
            )


def _wait_for_postgres(container: DockerContainer, timeout_seconds: float = 60) -> None:
    # The entrypoint's initialisation server listens on the Unix socket only; TCP readiness
    # means the final server is up.
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if container.exec(["pg_isready", "--host", "127.0.0.1", "--username", "postgres"]).exit_code == 0:
            return
        time.sleep(0.5)
    raise TimeoutError("PostgreSQL test container did not become ready")


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresServer]:
    superuser_password = secrets.token_urlsafe(24)
    role_passwords = {role: secrets.token_urlsafe(24) for role in ROLE_NAMES}
    container = (
        DockerContainer(POSTGRES_IMAGE)
        .with_env("POSTGRES_PASSWORD", superuser_password)
        .with_exposed_ports(5432)
        .with_volume_mapping(str(INFRA_DIR / "db"), "/hrms-bootstrap", "ro")
    )
    with container:
        _wait_for_postgres(container)
        result = container.exec(
            [
                "psql",
                "--username",
                "postgres",
                "--dbname",
                "postgres",
                "-v",
                f"db_name={DATABASE_NAME}",
                "-v",
                f"migrator_password={role_passwords['hrms_migrator']}",
                "-v",
                f"app_password={role_passwords['hrms_app']}",
                "-v",
                f"worker_password={role_passwords['hrms_worker']}",
                "-v",
                f"audit_retention_password={role_passwords['hrms_audit_retention']}",
                "-f",
                "/hrms-bootstrap/bootstrap-roles.sql",
            ]
        )
        assert result.exit_code == 0, result.output.decode()
        yield PostgresServer(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            superuser_password=superuser_password,
            role_passwords=role_passwords,
        )


def alembic_config(settings: MigrationSettings) -> AlembicConfig:
    config = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["settings"] = settings
    config.attributes["configure_logging"] = False
    return config


def migration_settings(url: SecretStr, app_env: AppEnv = AppEnv.TEST) -> MigrationSettings:
    return MigrationSettings(app_env=app_env, database_url_migrator=url)


@pytest.fixture(scope="session")
def migrated_postgres(postgres: PostgresServer) -> PostgresServer:
    """The `hrms` database at the head revision."""
    command.upgrade(alembic_config(migration_settings(postgres.url("hrms_migrator"))), "head")
    return postgres


def fresh_database(postgres: PostgresServer) -> str:
    """An empty database, as the cluster bootstrap would create it."""
    name = f"hrms_scratch_{secrets.token_hex(4)}"
    postgres.create_database(name)
    return name


def fresh_migrated_database(postgres: PostgresServer) -> str:
    """A database of its own at the head revision, for tests that alter the schema."""
    name = fresh_database(postgres)
    command.upgrade(alembic_config(migration_settings(postgres.url("hrms_migrator", name))), "head")
    return name


@asynccontextmanager
async def database_as(
    postgres: PostgresServer, role: str, name: str = DATABASE_NAME
) -> AsyncIterator[Database]:
    database = Database(create_engine(postgres.url(role, name), application_name=f"test-{role}"))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture(scope="session")
async def app_database(migrated_postgres: PostgresServer) -> AsyncIterator[Database]:
    """The shared test database, connected as the API's role."""
    async with database_as(migrated_postgres, "hrms_app") as database:
        yield database


@dataclass(frozen=True)
class RedisServer:
    host: str
    port: int
    password: str
    container: DockerContainer

    def url(self, user: str = "hrms_ratelimit", password: str | None = None) -> str:
        return f"redis://{user}:{password or self.password}@{self.host}:{self.port}/0"


@pytest.fixture(scope="session")
def redis() -> Iterator[RedisServer]:
    password = secrets.token_urlsafe(24)
    container = (
        DockerContainer(REDIS_IMAGE)
        .with_env("REDIS_RATELIMIT_PASSWORD", password)
        .with_exposed_ports(6379)
        .with_volume_mapping(str(INFRA_DIR / "redis"), "/hrms-redis", "ro")
        .with_kwargs(entrypoint=["/bin/sh", "/hrms-redis/start.sh"], user="redis")
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    )
    with container:
        yield RedisServer(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(6379)),
            password=password,
            container=container,
        )


@dataclass(frozen=True)
class S3Server:
    """The S3 emulator with Object Lock (infra/compose.yaml `rustfs`)."""

    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    region: str = "us-east-1"

    def client(self) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(s3={"addressing_style": "path"}),
        )

    def create_anchor_bucket(self, *, days: int = 1) -> str:
        """A fresh bucket with Object Lock and compliance-mode default retention, as
        scripts/create_local_anchor_bucket.py and infrastructure create it."""
        name = f"anchors-{secrets.token_hex(6)}"
        client = self.client()
        client.create_bucket(Bucket=name, ObjectLockEnabledForBucket=True)
        client.put_object_lock_configuration(
            Bucket=name,
            ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": days}},
            },
        )
        return name

    def anchor_store(self, bucket: str) -> S3AnchorStore:
        return S3AnchorStore(
            bucket=bucket,
            region=self.region,
            access_key_id=SecretStr(self.access_key_id),
            secret_access_key=SecretStr(self.secret_access_key),
            endpoint_url=self.endpoint_url,
        )


def _wait_for_s3(url: str, timeout_seconds: float = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise TimeoutError("RustFS test container did not become ready")


@pytest.fixture(scope="session")
def s3() -> Iterator[S3Server]:
    access_key, secret_key = f"test{secrets.token_hex(6)}", secrets.token_urlsafe(24)
    container = (
        DockerContainer(RUSTFS_IMAGE)
        .with_env("RUSTFS_ACCESS_KEY", access_key)
        .with_env("RUSTFS_SECRET_KEY", secret_key)
        .with_exposed_ports(9000)
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        _wait_for_s3(url)
        yield S3Server(url, access_key, secret_key)


@pytest.fixture
async def api(migrated_postgres: PostgresServer, redis: RedisServer) -> AsyncIterator["Api"]:
    """A running API on the shared test database (tests/api_support.py)."""
    from tests.api_support import running_api  # noqa: PLC0415 (api_support imports this module)

    async with running_api(migrated_postgres, redis) as running:
        yield running


@pytest.fixture
async def isolated_api(migrated_postgres: PostgresServer, redis: RedisServer) -> AsyncIterator["Api"]:
    """A running API on a database of its own, for tests whose assertions depend on everything
    in a table, such as which outbox rows one dispatcher run claims. Rows other tests leave in
    the shared database would otherwise compete for the same batch."""
    from tests.api_support import running_api  # noqa: PLC0415 (api_support imports this module)

    async with running_api(
        migrated_postgres, redis, database_name=fresh_migrated_database(migrated_postgres)
    ) as running:
        yield running
