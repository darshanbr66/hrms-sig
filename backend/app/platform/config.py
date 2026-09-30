"""Typed settings loaded from environment variables, validated at startup.

Each process loads only the settings it needs, so the API never holds the worker's
or the migrator's database credentials (docs/security-architecture.md §6.4).
"""

import base64
import binascii
import ipaddress
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

MIN_HMAC_KEY_BYTES = 32


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_deployed(self) -> bool:
        return self in (AppEnv.STAGING, AppEnv.PRODUCTION)


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    app_env: AppEnv
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _refuse_debug_when_deployed(self) -> Self:
        if self.app_env.is_deployed and self.log_level == "DEBUG":
            raise ValueError("LOG_LEVEL=DEBUG is not allowed in staging or production")
        return self


def _require_postgres_url(value: SecretStr, name: str, app_env: AppEnv) -> None:
    parts = urlsplit(value.get_secret_value())
    if parts.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"{name} must be a postgresql:// URL")
    if app_env.is_deployed:
        sslmode = parse_qs(parts.query).get("sslmode", [""])[0]
        if sslmode != "verify-full":
            raise ValueError(f"{name} must use sslmode=verify-full in staging and production")


def sqlalchemy_url(url: SecretStr) -> str:
    """Return the URL with the psycopg 3 driver selected for SQLAlchemy."""
    raw = url.get_secret_value()
    scheme, rest = raw.split("://", 1)
    if scheme not in ("postgresql", "postgres"):
        raise ValueError("expected a postgresql:// URL")
    return f"postgresql+psycopg://{rest}"


class ApiSettings(_Base):
    """Settings for the API process."""

    database_url_app: SecretStr
    redis_url: SecretStr
    rate_limit_key_hmac_key: SecretStr
    trusted_proxy_cidrs: Annotated[tuple[IPNetwork, ...], NoDecode] = ()
    api_docs_enabled: bool = False

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _split_cidrs(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(ipaddress.ip_network(item.strip()) for item in value.split(",") if item.strip())
        return value

    @field_validator("rate_limit_key_hmac_key")
    @classmethod
    def _check_hmac_key(cls, value: SecretStr) -> SecretStr:
        try:
            decoded = base64.b64decode(value.get_secret_value(), validate=True)
        except binascii.Error as exc:
            raise ValueError("RATE_LIMIT_KEY_HMAC_KEY must be base64") from exc
        if len(decoded) < MIN_HMAC_KEY_BYTES:
            raise ValueError(f"RATE_LIMIT_KEY_HMAC_KEY must decode to at least {MIN_HMAC_KEY_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_app, "DATABASE_URL_APP", self.app_env)
        scheme = urlsplit(self.redis_url.get_secret_value()).scheme
        if scheme not in ("redis", "rediss"):
            raise ValueError("REDIS_URL must be a redis:// or rediss:// URL")
        if self.app_env.is_deployed:
            if scheme != "rediss":
                raise ValueError("REDIS_URL must use TLS (rediss://) in staging and production")
            if self.api_docs_enabled:
                raise ValueError("API_DOCS_ENABLED is not allowed in staging or production")
        return self

    @property
    def rate_limit_hmac_key_bytes(self) -> bytes:
        return base64.b64decode(self.rate_limit_key_hmac_key.get_secret_value())


class WorkerSettings(_Base):
    """Settings for the worker process."""

    database_url_worker: SecretStr

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_worker, "DATABASE_URL_WORKER", self.app_env)
        return self


class MigrationSettings(_Base):
    """Settings for the one-off migration job (Alembic as hrms_migrator)."""

    database_url_migrator: SecretStr

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_migrator, "DATABASE_URL_MIGRATOR", self.app_env)
        return self
