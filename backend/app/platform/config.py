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

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.platform.audit.signing import CheckpointSigner, SignatureVerifier
from app.platform.files import S3FileStore
from app.platform.storage import S3AnchorStore

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
        # A refused setting is reported by name and reason only. Without this, the error a
        # process prints at startup includes the offending value (a key, a password in a
        # URL) and, for a cross-field rule, part of the whole environment, into the logs.
        hide_input_in_errors=True,
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


def _decode_hmac_key(value: SecretStr, name: str) -> bytes:
    try:
        decoded = base64.b64decode(value.get_secret_value(), validate=True)
    except binascii.Error as exc:
        raise ValueError(f"{name} must be base64") from exc
    if len(decoded) < MIN_HMAC_KEY_BYTES:
        raise ValueError(f"{name} must decode to at least {MIN_HMAC_KEY_BYTES} bytes")
    return decoded


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


class _FileStorage(_Base):
    """The private bucket for documents and export files (app/platform/files.py). Optional
    locally, where the API then says file storage is not set up; required in staging and
    production. Not the audit anchor bucket, and never with the anchor credentials there."""

    files_bucket: str | None = Field(default=None, min_length=3, max_length=63)
    files_access_key_id: SecretStr | None = None
    files_secret_access_key: SecretStr | None = None
    # Shared with the anchor settings in the worker: empty for AWS S3, the emulator locally.
    s3_endpoint_url: str | None = None
    s3_region: str | None = None

    @field_validator(
        "files_bucket", "files_access_key_id", "files_secret_access_key", "s3_region", mode="before"
    )
    @classmethod
    def _blank_file_setting_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_file_storage(self) -> Self:
        given = [self.files_bucket, self.files_access_key_id, self.files_secret_access_key]
        if any(value is not None for value in given) and not all(value is not None for value in given):
            raise ValueError("FILES_BUCKET, FILES_ACCESS_KEY_ID and FILES_SECRET_ACCESS_KEY are set together")
        if self.files_bucket is not None and not self.s3_region:
            raise ValueError("S3_REGION is required with FILES_BUCKET")
        if self.files_bucket is None and self.app_env.is_deployed:
            raise ValueError("FILES_BUCKET is required in staging and production (documents and exports)")
        if self.s3_endpoint_url:
            scheme = urlsplit(self.s3_endpoint_url).scheme
            if scheme not in ("http", "https"):
                raise ValueError("S3_ENDPOINT_URL must be an http(s) URL")
            if self.app_env.is_deployed and scheme != "https":
                raise ValueError("S3_ENDPOINT_URL must use https in staging and production")
        return self

    def file_store(self) -> S3FileStore | None:
        if (
            self.files_bucket is None
            or self.files_access_key_id is None
            or self.files_secret_access_key is None
        ):
            return None
        return S3FileStore(
            bucket=self.files_bucket,
            region=self.s3_region or "",
            access_key_id=self.files_access_key_id,
            secret_access_key=self.files_secret_access_key,
            endpoint_url=self.s3_endpoint_url or None,
        )


class ApiSettings(_WithAppBaseUrl, _FileStorage):
    """Settings for the API process."""

    database_url_app: SecretStr
    redis_url: SecretStr
    rate_limit_key_hmac_key: SecretStr
    # Keyed hash of attempted email addresses (platform/security/emails.py); its own key.
    email_lookup_hmac_key: SecretStr
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

    @field_validator("rate_limit_key_hmac_key", "email_lookup_hmac_key")
    @classmethod
    def _check_hmac_key(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        _decode_hmac_key(value, (info.field_name or "key").upper())
        return value

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_app, "DATABASE_URL_APP", self.app_env)
        if self.rate_limit_hmac_key_bytes == self.email_lookup_key_bytes:
            raise ValueError(
                "EMAIL_LOOKUP_HMAC_KEY must differ from RATE_LIMIT_KEY_HMAC_KEY (one key per purpose)"
            )
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
    def email_lookup_key_bytes(self) -> bytes:
        return base64.b64decode(self.email_lookup_hmac_key.get_secret_value())

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


class SmtpSecurity(StrEnum):
    STARTTLS = "starttls"
    TLS = "tls"
    # Plain SMTP, for the local mail catcher only.
    NONE = "none"


class _AuditAnchors(_Base):
    """Reading the audit chain's write-once anchors and checking checkpoint signatures
    (docs/security-architecture.md §8.1). Public keys only: no signing key here."""

    # {"<key id>": "<base64 raw Ed25519 public key>"}; old keys stay listed after rotation.
    audit_verify_public_keys: str
    # Empty for AWS S3; the emulator's URL locally.
    s3_endpoint_url: str | None = None
    s3_region: str
    audit_anchor_bucket: str = Field(min_length=3, max_length=63)
    audit_anchor_access_key_id: SecretStr
    audit_anchor_secret_access_key: SecretStr

    @field_validator("s3_endpoint_url", mode="before")
    @classmethod
    def _blank_endpoint_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_anchor_settings(self) -> Self:
        self.signature_verifier()
        if self.s3_endpoint_url is not None:
            scheme = urlsplit(self.s3_endpoint_url).scheme
            if scheme not in ("http", "https"):
                raise ValueError("S3_ENDPOINT_URL must be an http(s) URL")
            if self.app_env.is_deployed and scheme != "https":
                raise ValueError("S3_ENDPOINT_URL must use https in staging and production")
        return self

    def signature_verifier(self) -> SignatureVerifier:
        return SignatureVerifier.from_json(self.audit_verify_public_keys)

    def anchor_store(self) -> S3AnchorStore:
        return S3AnchorStore(
            bucket=self.audit_anchor_bucket,
            region=self.s3_region,
            access_key_id=self.audit_anchor_access_key_id,
            secret_access_key=self.audit_anchor_secret_access_key,
            endpoint_url=self.s3_endpoint_url,
        )


class AuditVerifySettings(_AuditAnchors):
    """Settings for `python -m app.cli audit-verify`: read-only, as the worker's database role."""

    database_url_worker: SecretStr

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_worker, "DATABASE_URL_WORKER", self.app_env)
        return self


type RetentionMonths = Annotated[int, Field(ge=1, le=1200)]


class WorkerSettings(_WithAppBaseUrl, _AuditAnchors, _FileStorage):
    """Settings for the worker process. It sends the email outbox, so it holds the SMTP
    credentials and builds links from APP_BASE_URL (never from a request). It alone holds the
    audit checkpoint signing key, and runs audit retention as its own database role."""

    database_url_worker: SecretStr
    database_url_audit_retention: SecretStr
    audit_signing_private_key: SecretStr
    audit_signing_key_id: str
    # Months each audit stream is kept; unset means nothing is removed (no value assumed,
    # docs/database-design.md §9).
    audit_log_retention_months: RetentionMonths | None = None
    security_events_retention_months: RetentionMonths | None = None
    smtp_host: str
    smtp_port: int
    smtp_from: str
    smtp_security: SmtpSecurity
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    # The ClamAV daemon that scans uploaded documents (DOC-5). Unset: uploads stay waiting
    # for a scan and cannot be downloaded (fail closed). Required in staging and production.
    clamav_host: str | None = None
    clamav_port: int = 3310

    @field_validator(
        "clamav_host",
        "smtp_username",
        "smtp_password",
        "audit_log_retention_months",
        "security_events_retention_months",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_worker, "DATABASE_URL_WORKER", self.app_env)
        _require_postgres_url(self.database_url_audit_retention, "DATABASE_URL_AUDIT_RETENTION", self.app_env)
        if not self.signature_verifier().matches(self.checkpoint_signer()):
            raise ValueError(
                "AUDIT_VERIFY_PUBLIC_KEYS must list the public key of AUDIT_SIGNING_PRIVATE_KEY "
                "under AUDIT_SIGNING_KEY_ID"
            )
        if self.smtp_security is SmtpSecurity.NONE and self.app_env not in (AppEnv.LOCAL, AppEnv.TEST):
            raise ValueError("SMTP_SECURITY=none is allowed only in local and test")
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD are set together or not at all")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be a TCP port")
        if not 1 <= self.clamav_port <= 65535:
            raise ValueError("CLAMAV_PORT must be a TCP port")
        if self.app_env.is_deployed and self.clamav_host is None:
            raise ValueError("CLAMAV_HOST is required in staging and production (document scanning)")
        return self

    def checkpoint_signer(self) -> CheckpointSigner:
        return CheckpointSigner.from_base64(
            self.audit_signing_key_id, self.audit_signing_private_key.get_secret_value()
        )


class MigrationSettings(_Base):
    """Settings for the one-off migration job (Alembic as hrms_migrator)."""

    database_url_migrator: SecretStr

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        _require_postgres_url(self.database_url_migrator, "DATABASE_URL_MIGRATOR", self.app_env)
        return self
