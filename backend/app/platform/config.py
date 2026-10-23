"""Typed settings loaded from environment variables, validated at startup.

Each process loads only the settings it needs, and each connects to MongoDB as its own
database user (docs/security-architecture.md §6.4): the API, the worker, the audit retention
job and the migration command each get a `MONGODB_URI` naming a different Atlas user with only
the privileges that process needs (docs/deployment.md §4). When the worker's jobs run inside
the API process (`RUN_JOBS_IN_API`), they keep the worker's user (`MONGODB_WORKER_URI`).
"""

import base64
import binascii
import ipaddress
import json
from email.utils import parseaddr
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import EmailStr, Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.platform.audit.signing import CheckpointSigner, SignatureVerifier

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

MIN_HMAC_KEY_BYTES = 32
FIELD_KEY_BYTES = 32
# The production database's name (ADR-030). Local and test processes refuse it, so a command run
# from a developer's shell cannot write into production by a stale or copied MONGODB_DATABASE.
PRODUCTION_DATABASE = "hrms"
# Query options that switch TLS off or stop certificate checks; refused when deployed.
_INSECURE_OPTIONS = {
    "tls": "false",
    "ssl": "false",
    "tlsinsecure": "true",
    "tlsallowinvalidcertificates": "true",
    "tlsallowinvalidhostnames": "true",
    "tlsdisableocspendpointcheck": "true",
}


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
        # URI) and, for a cross-field rule, part of the whole environment, into the logs.
        hide_input_in_errors=True,
    )

    app_env: AppEnv
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _refuse_debug_when_deployed(self) -> Self:
        if self.app_env.is_deployed and self.log_level == "DEBUG":
            raise ValueError("LOG_LEVEL=DEBUG is not allowed in staging or production")
        return self


def check_mongodb_uri(value: SecretStr, name: str, app_env: AppEnv) -> None:
    """A MongoDB connection string; when deployed: authenticated and over verified TLS."""
    parts = urlsplit(value.get_secret_value())
    if parts.scheme not in ("mongodb", "mongodb+srv") or not parts.hostname:
        raise ValueError(f"{name} must be a mongodb:// or mongodb+srv:// URI")
    if not app_env.is_deployed:
        return
    options = {key.lower(): values[-1].lower() for key, values in parse_qs(parts.query).items()}
    for option, insecure in _INSECURE_OPTIONS.items():
        if options.get(option) == insecure:
            raise ValueError(f"{name} must not set {option}={insecure} in staging and production")
    # mongodb+srv implies TLS; a plain mongodb:// URI must ask for it.
    if parts.scheme == "mongodb" and "true" not in (options.get("tls"), options.get("ssl")):
        raise ValueError(f"{name} must use TLS (mongodb+srv:// or tls=true) in staging and production")
    if not parts.username and options.get("authmechanism") != "mongodb-x509":
        raise ValueError(f"{name} must name a database user in staging and production")


def same_database_user(first: SecretStr, second: SecretStr) -> bool:
    """Whether two connection strings sign in as the same database user, however written
    (another host list, options or quoting). Without user names (X.509), the strings decide."""
    a = urlsplit(first.get_secret_value()).username
    b = urlsplit(second.get_secret_value()).username
    if a is None and b is None:
        return first.get_secret_value() == second.get_secret_value()
    return a is not None and b is not None and unquote(a) == unquote(b)


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


class _WithMongo(_Base):
    """The process's own database user (`MONGODB_URI`) and the database name."""

    mongodb_uri: SecretStr
    mongodb_database: str = Field(pattern=r"^[A-Za-z0-9_-]{1,63}$")

    @model_validator(mode="after")
    def _check_mongodb(self) -> Self:
        check_mongodb_uri(self.mongodb_uri, "MONGODB_URI", self.app_env)
        check_database_name(self.mongodb_database, self.app_env)
        return self


def check_database_name(name: str, app_env: AppEnv) -> None:
    """Only production uses the production database's name: local, test and staging never do."""
    if app_env is not AppEnv.PRODUCTION and name.lower() == PRODUCTION_DATABASE:
        raise ValueError(
            f"MONGODB_DATABASE={PRODUCTION_DATABASE} is the production database; local, test and "
            "staging runs use another name (for example hrms_local or hrms_staging)"
        )


class _WithAppBaseUrl(_Base):
    # The SPA's origin: unsafe requests must come from it, and links are built from it.
    app_base_url: str

    @field_validator("app_base_url")
    @classmethod
    def _check_origin(cls, value: str, info: ValidationInfo) -> str:
        app_env = info.data.get("app_env")
        return _check_app_base_url(value, app_env if isinstance(app_env, AppEnv) else AppEnv.PRODUCTION)


class _Uploads(_Base):
    # False refuses document uploads, for a deployment without the ClamAV daemon: nothing is
    # accepted that could not be scanned (DOC-5). Off unless turned on; the same value on the
    # API and the worker.
    document_uploads_enabled: bool = False


class ApiSettings(_WithMongo, _WithAppBaseUrl, _Uploads):
    """Settings for the API process."""

    rate_limit_key_hmac_key: SecretStr
    # Keyed hash of attempted email addresses (platform/security/emails.py); its own key.
    email_lookup_hmac_key: SecretStr
    # Signs the 60-second download links of documents and exports (platform/files.py).
    download_link_hmac_key: SecretStr
    field_encryption_keys: SecretStr
    field_encryption_active_version: int
    hibp_enabled: bool = True
    trusted_proxy_cidrs: Annotated[tuple[IPNetwork, ...], NoDecode] = ()
    api_docs_enabled: bool = False
    # Runs the worker's scheduled jobs inside the API process (ADR-030), for a deployment with
    # one web service and no separate worker. The jobs then read `JobsInApiSettings`.
    run_jobs_in_api: bool = False
    # Office location verification (ADR-031). Off unless turned on, in every environment: no
    # office check can be created or versioned, and none is applied to clock events, so nothing
    # is asked of browsers and no evidence is written; clocking works as without the module. An
    # environment setting, so no role or permission can turn it on. Turning it on in production
    # is prohibited until the evidence retention purge, the privacy notice approval and the
    # one-office pilot are in place (docs/module-acceptance.md §15.1).
    location_checks_enabled: bool = False

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _split_cidrs(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(ipaddress.ip_network(item.strip()) for item in value.split(",") if item.strip())
        return value

    @field_validator("rate_limit_key_hmac_key", "email_lookup_hmac_key", "download_link_hmac_key")
    @classmethod
    def _check_hmac_key(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        _decode_hmac_key(value, (info.field_name or "key").upper())
        return value

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        keys = [self.rate_limit_hmac_key_bytes, self.email_lookup_key_bytes, self.download_link_key_bytes]
        if len(set(keys)) != len(keys):
            raise ValueError(
                "RATE_LIMIT_KEY_HMAC_KEY, EMAIL_LOOKUP_HMAC_KEY and DOWNLOAD_LINK_HMAC_KEY must differ "
                "(one key per purpose)"
            )
        if self.field_encryption_active_version not in _decode_field_keys(self.field_encryption_keys):
            raise ValueError("FIELD_ENCRYPTION_ACTIVE_VERSION must name a key in FIELD_ENCRYPTION_KEYS")
        if self.app_env.is_deployed and self.api_docs_enabled:
            raise ValueError("API_DOCS_ENABLED is not allowed in staging or production")
        return self

    @property
    def rate_limit_hmac_key_bytes(self) -> bytes:
        return base64.b64decode(self.rate_limit_key_hmac_key.get_secret_value())

    @property
    def email_lookup_key_bytes(self) -> bytes:
        return base64.b64decode(self.email_lookup_hmac_key.get_secret_value())

    @property
    def download_link_key_bytes(self) -> bytes:
        return base64.b64decode(self.download_link_hmac_key.get_secret_value())

    @property
    def field_keys(self) -> dict[int, bytes]:
        return _decode_field_keys(self.field_encryption_keys)


class CliSettings(_WithMongo, _WithAppBaseUrl):
    """Settings for operator commands (app/cli.py), which act as the API's database user."""


class EmailTransport(StrEnum):
    # Resend's HTTPS API: the transport of every deployed environment.
    RESEND = "resend"
    # SMTP, for the local mail catcher (Mailpit) and tests only.
    SMTP = "smtp"


class SmtpSecurity(StrEnum):
    STARTTLS = "starttls"
    TLS = "tls"
    # Plain SMTP, for the local mail catcher only.
    NONE = "none"


class _AuditVerification(_Base):
    """Checking checkpoint signatures (docs/security-architecture.md §8.1). Public keys only."""

    # {"<key id>": "<base64 raw Ed25519 public key>"}; old keys stay listed after rotation.
    audit_verify_public_keys: str

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        self.signature_verifier()
        return self

    def signature_verifier(self) -> SignatureVerifier:
        return SignatureVerifier.from_json(self.audit_verify_public_keys)


class AuditVerifySettings(_WithMongo, _AuditVerification):
    """Settings for `python -m app.cli audit-verify`: read-only, as the worker's database user."""


type RetentionMonths = Annotated[int, Field(ge=1, le=1200)]


def _check_sender(value: str) -> str:
    """`EMAIL_FROM`: an address, optionally with a display name (`HRMS <hrms@example.com>`)."""
    if any(char in value for char in "\r\n"):
        raise ValueError("EMAIL_FROM must be one line")
    _, address = parseaddr(value)
    local, _, domain = address.rpartition("@")
    if not local or not domain or any(char.isspace() for char in address):
        raise ValueError("EMAIL_FROM must be an email address, optionally with a display name")
    return value


class WorkerSettings(_WithMongo, _WithAppBaseUrl, _AuditVerification, _Uploads):
    """Settings for the worker process. It sends the email outbox, so it holds the email
    credentials (`RESEND_API_KEY`) and builds links from APP_BASE_URL (never from a request). It
    alone holds the audit checkpoint signing key. Audit retention, when configured, runs as its
    own database user (`MONGODB_AUDIT_RETENTION_URI`), the only one allowed to remove audit
    records. In production the daily audit witness must have recipients
    (`AUDIT_WITNESS_EMAILS`): without the witness nothing outside the database shows that the
    newest audit records were removed."""

    mongodb_audit_retention_uri: SecretStr | None = None
    audit_signing_private_key: SecretStr
    audit_signing_key_id: str
    # Months each audit stream is kept; unset means nothing is removed (no value assumed,
    # docs/database-design.md §9).
    audit_log_retention_months: RetentionMonths | None = None
    security_events_retention_months: RetentionMonths | None = None
    # Mailboxes outside the HRMS that receive the daily audit witness (signed checkpoint heads,
    # docs/security-architecture.md §8.1). Required in production; empty elsewhere means no
    # witness is sent and the audit trail is not protected against truncation by someone with
    # database access.
    audit_witness_emails: Annotated[tuple[EmailStr, ...], NoDecode] = ()
    email_transport: EmailTransport = EmailTransport.RESEND
    email_from: str
    resend_api_key: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: SmtpSecurity = SmtpSecurity.STARTTLS
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    # The ClamAV daemon that scans uploaded documents (DOC-5). Required while uploads are on.
    clamav_host: str | None = None
    clamav_port: int = 3310

    @field_validator(
        "clamav_host",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "resend_api_key",
        "mongodb_audit_retention_uri",
        "audit_log_retention_months",
        "security_events_retention_months",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("audit_witness_emails", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("email_from")
    @classmethod
    def _check_email_from(cls, value: str) -> str:
        return _check_sender(value)

    @model_validator(mode="after")
    def _check_deployed_requirements(self) -> Self:
        if not self.signature_verifier().matches(self.checkpoint_signer()):
            raise ValueError(
                "AUDIT_VERIFY_PUBLIC_KEYS must list the public key of AUDIT_SIGNING_PRIVATE_KEY "
                "under AUDIT_SIGNING_KEY_ID"
            )
        self._check_retention()
        self._check_email()
        if not 1 <= self.clamav_port <= 65535:
            raise ValueError("CLAMAV_PORT must be a TCP port")
        if self.app_env.is_deployed and self.clamav_host is None and self.document_uploads_enabled:
            raise ValueError(
                "CLAMAV_HOST is required in staging and production (document scanning) "
                "unless DOCUMENT_UPLOADS_ENABLED=false"
            )
        if self.app_env is AppEnv.PRODUCTION and not self.audit_witness_emails:
            raise ValueError(
                "AUDIT_WITNESS_EMAILS is required in production: at least one mailbox outside the "
                "HRMS administrators' control that receives the daily audit witness"
            )
        return self

    @property
    def retention_configured(self) -> bool:
        return (
            self.audit_log_retention_months is not None
            or self.security_events_retention_months is not None
            or self.mongodb_audit_retention_uri is not None
        )

    def _check_retention(self) -> None:
        retention_set = (
            self.audit_log_retention_months is not None or self.security_events_retention_months is not None
        )
        if retention_set and self.mongodb_audit_retention_uri is None:
            raise ValueError("MONGODB_AUDIT_RETENTION_URI is required when an audit retention period is set")
        if self.mongodb_audit_retention_uri is not None:
            check_mongodb_uri(self.mongodb_audit_retention_uri, "MONGODB_AUDIT_RETENTION_URI", self.app_env)
            if self.app_env.is_deployed and same_database_user(
                self.mongodb_audit_retention_uri, self.mongodb_uri
            ):
                raise ValueError(
                    "MONGODB_AUDIT_RETENTION_URI must name another database user than MONGODB_URI"
                )

    def _check_email(self) -> None:
        if self.email_transport is EmailTransport.RESEND:
            if self.resend_api_key is None:
                raise ValueError("RESEND_API_KEY is required when EMAIL_TRANSPORT=resend")
            return
        if self.app_env not in (AppEnv.LOCAL, AppEnv.TEST):
            raise ValueError("EMAIL_TRANSPORT=smtp is allowed only in local and test; use resend")
        if self.smtp_host is None:
            raise ValueError("SMTP_HOST is required when EMAIL_TRANSPORT=smtp")
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD are set together or not at all")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be a TCP port")

    def checkpoint_signer(self) -> CheckpointSigner:
        return CheckpointSigner.from_base64(
            self.audit_signing_key_id, self.audit_signing_private_key.get_secret_value()
        )


class JobsInApiSettings(WorkerSettings):
    """The worker's settings when its jobs run inside the API process (`RUN_JOBS_IN_API=true`).

    The jobs connect with `MONGODB_WORKER_URI`, on connections of their own. It may name the
    same database user and URI as `MONGODB_URI` (that user then needs the worker's privileges);
    it is still required in staging and production, so the jobs' connection is always stated.

    Audit retention never runs inside the internet-facing API in staging and production: the
    retention user is the only one that can remove audit records, and the process already holds
    the signing key. `MONGODB_AUDIT_RETENTION_URI` and both retention periods are refused there,
    so retention stays off until a separate worker (`RUN_JOBS_IN_API=false`, `python -m
    app.worker`) runs it (docs/deployment.md, B5)."""

    mongodb_worker_uri: SecretStr | None = None

    @field_validator("mongodb_worker_uri", mode="before")
    @classmethod
    def _blank_worker_uri(cls, value: object) -> object:
        return None if value == "" else value

    def _check_retention(self) -> None:
        if self.app_env.is_deployed and self.retention_configured:
            raise ValueError(
                "audit retention does not run inside the API (RUN_JOBS_IN_API=true) in staging and "
                "production: leave MONGODB_AUDIT_RETENTION_URI, AUDIT_LOG_RETENTION_MONTHS and "
                "SECURITY_EVENTS_RETENTION_MONTHS unset, or run a separate worker"
            )
        super()._check_retention()

    @model_validator(mode="after")
    def _check_worker_uri(self) -> Self:
        if self.mongodb_worker_uri is None:
            if self.app_env.is_deployed:
                raise ValueError(
                    "MONGODB_WORKER_URI is required in staging and production with RUN_JOBS_IN_API"
                )
            return self
        check_mongodb_uri(self.mongodb_worker_uri, "MONGODB_WORKER_URI", self.app_env)
        return self

    @property
    def jobs_mongodb_uri(self) -> SecretStr:
        return self.mongodb_worker_uri or self.mongodb_uri


class MigrationSettings(_WithMongo):
    """Settings for `python -m app.cli migrate`, as the migration database user."""
