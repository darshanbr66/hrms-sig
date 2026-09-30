"""Shared fixtures: real PostgreSQL 18 and Redis containers, set up with the same bootstrap
SQL and ACL files as the local Compose environment (infra/)."""

import asyncio
import secrets
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.platform.config import AppEnv, MigrationSettings

BACKEND_DIR = Path(__file__).resolve().parents[1]
INFRA_DIR = BACKEND_DIR.parent / "infra"

POSTGRES_IMAGE = "postgres:18.6"
REDIS_IMAGE = "redis:7.2.16"

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


def alembic_config(settings: MigrationSettings) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
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
