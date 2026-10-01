"""Typed settings loaded from environment variables, validated at startup.

Each process loads only the settings it needs, so the API never holds the worker's
or the migrator's database credentials (docs/security-architecture.md §6.4).
"""

import base64
import binascii
import ipaddress
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

MIN_HMAC_KEY_BYTES = 32
FIELD_KEY_BYTES = 32


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


def _check_app_base_url(value: str, app_env: AppEnv) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("APP_BASE_URL must be an absolute http(s) URL")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("APP_BASE_URL must be an origin without a path, query or fragment")
    if app_env.is_deployed and parts.scheme != "https":
        raise ValueError("APP_BASE_URL must use https in staging and production")
    return f"{parts.scheme}://{parts.netloc}"


def _decode_field_keys(value: SecretStr) -> dict[int, bytes]:
    try:
        raw = json.loads(value.get_secret_value())
        keys = {int(version): base64.b64decode(key, validate=True) for version, key in raw.items()}
    except (ValueError, AttributeError, TypeError, binascii.Error) as exc:
        raise ValueError('FIELD_ENCRYPTION_KEYS must be a JSON object {"<version>": "<base64 key>"}') from exc
    if not keys or any(len(key) != FIELD_KEY_BYTES for key in keys.values()):
        raise ValueError(f"every FIELD_ENCRYPTION_KEYS key must decode to {FIELD_KEY_BYTES} bytes")
    return keys


class _WithAppBaseUrl(_Base):
    # The SPA's origin: unsafe requests must come from it, and links are built from it.
    app_base_url: str

    @field_validator("app_base_url")
    @classmethod
    def _check_origin(cls, value: str, info: ValidationInfo) -> str:
        app_env = info.data.get("app_env")
        return _check_app_base_url(value, app_env if isinstance(app_env, AppEnv) else AppEnv.PRODUCTION)


class ApiSettings(_WithAppBaseUrl):
    """Settings for the API process."""

    database_url_app: SecretStr
    redis_url: SecretStr
    rate_limit_key_hmac_key: SecretStr
    field_encryption_keys: SecretStr
    field_encryption_active_version: int
    hibp_enabled: bool = True
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
        if self.field_encryption_active_version not in _decode_field_keys(self.field_encryption_keys):
            raise ValueError("FIELD_ENCRYPTION_ACTIVE_VERSION must name a key in FIELD_ENCRYPTION_KEYS")
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

    @property
    def field_keys(self) -> dict[int, bytes]:
        return _decode_field_keys(self.field_encryption_keys)


class CliSettings(_WithAppBaseUrl):
    """Settings for operator commands (app/cli.py), which act as the API's database role."""

    database_url_app: SecretStr

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_app, "DATABASE_URL_APP", self.app_env)
        return self


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
