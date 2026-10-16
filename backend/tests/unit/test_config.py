import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import ValidationError

from app.platform.config import ApiSettings, AppEnv, MigrationSettings, WorkerSettings, sqlalchemy_url

HMAC_KEY = base64.b64encode(b"k" * 32).decode()
EMAIL_KEY = base64.b64encode(b"m" * 32).decode()
FIELD_KEY = base64.b64encode(b"f" * 32).decode()
FIELD_KEYS = f'{{"1": "{FIELD_KEY}"}}'
LOCAL_DB = "postgresql://hrms_app:pw@127.0.0.1:5433/hrms"
SIGNING_SEED = b"s" * 32
PUBLIC_KEY = base64.b64encode(
    Ed25519PrivateKey.from_private_bytes(SIGNING_SEED)
    .public_key()
    .public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode()
# Everything a worker needs besides its environment and database URL.
WORKER: dict[str, object] = {
    "app_base_url": "https://hrms.example.com",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "smtp_from": "hrms@example.com",
    "smtp_security": "starttls",
    "database_url_audit_retention": "postgresql://hrms_audit_retention:pw@db.internal:5432/hrms?sslmode=verify-full",
    "audit_signing_key_id": "key-1",
    "audit_signing_private_key": base64.b64encode(SIGNING_SEED).decode(),
    "audit_verify_public_keys": json.dumps({"key-1": PUBLIC_KEY}),
    "s3_endpoint_url": "",
    "s3_region": "ap-south-1",
    "audit_anchor_bucket": "hrms-audit-anchors",
    "audit_anchor_access_key_id": "anchor-writer",
    "audit_anchor_secret_access_key": "anchor-secret",
    "files_bucket": "hrms-files",
    "files_access_key_id": "files-writer",
    "files_secret_access_key": "files-secret",
    "clamav_host": "clamav.internal",
}
# The private bucket for documents and exports.
FILES: dict[str, object] = {
    "files_bucket": "hrms-files",
    "files_access_key_id": "files-writer",
    "files_secret_access_key": "files-secret",
    "s3_region": "ap-south-1",
}
DEPLOYED_DB = "postgresql://hrms_app:pw@db.internal:5432/hrms?sslmode=verify-full"


def api_settings(**overrides: object) -> ApiSettings:
    values: dict[str, object] = {
        "app_env": AppEnv.LOCAL,
        "database_url_app": LOCAL_DB,
        "redis_url": "redis://hrms_ratelimit:pw@127.0.0.1:6380/0",
        "rate_limit_key_hmac_key": HMAC_KEY,
        "email_lookup_hmac_key": EMAIL_KEY,
        "app_base_url": "http://localhost:5173",
        "field_encryption_keys": FIELD_KEYS,
        "field_encryption_active_version": 1,
        **FILES,
    }
    values.update(overrides)
    return ApiSettings.model_validate(values)


def test_local_settings_are_accepted() -> None:
    settings = api_settings(trusted_proxy_cidrs="10.0.0.0/8, 192.168.1.0/24")
    assert [str(network) for network in settings.trusted_proxy_cidrs] == ["10.0.0.0/8", "192.168.1.0/24"]
    assert settings.rate_limit_hmac_key_bytes == b"k" * 32


def test_production_settings_with_tls_are_accepted() -> None:
    settings = api_settings(
        app_env=AppEnv.PRODUCTION,
        database_url_app=DEPLOYED_DB,
        redis_url="rediss://hrms_ratelimit:pw@cache.internal:6379/0",
        app_base_url="https://hrms.example.com/",
    )
    assert settings.app_env.is_deployed
    assert settings.app_base_url == "https://hrms.example.com"


@pytest.mark.parametrize("app_env", [AppEnv.STAGING, AppEnv.PRODUCTION])
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"database_url_app": LOCAL_DB}, "sslmode=verify-full"),
        ({"database_url_app": DEPLOYED_DB.replace("verify-full", "require")}, "sslmode=verify-full"),
        ({"redis_url": "redis://hrms_ratelimit:pw@cache.internal:6379/0"}, "rediss://"),
        ({"api_docs_enabled": True}, "API_DOCS_ENABLED"),
        ({"log_level": "DEBUG"}, "DEBUG"),
        ({"app_base_url": "http://hrms.example.com"}, "APP_BASE_URL"),
    ],
)
def test_deployed_environments_refuse_unsafe_settings(
    app_env: AppEnv, overrides: dict[str, object], message: str
) -> None:
    safe: dict[str, object] = {
        "app_env": app_env,
        "database_url_app": DEPLOYED_DB,
        "redis_url": "rediss://hrms_ratelimit:pw@cache.internal:6379/0",
        "app_base_url": "https://hrms.example.com",
    }
    with pytest.raises(ValidationError, match=message):
        api_settings(**(safe | overrides))


@pytest.mark.parametrize(
    "key",
    ["not base64!", base64.b64encode(b"short").decode()],
)
def test_weak_or_malformed_hmac_key_is_refused(key: str) -> None:
    with pytest.raises(ValidationError, match="RATE_LIMIT_KEY_HMAC_KEY"):
        api_settings(rate_limit_key_hmac_key=key)


@pytest.mark.parametrize("url", ["localhost:5173", "http://localhost:5173/app", "http://localhost?x=1"])
def test_app_base_url_must_be_an_origin(url: str) -> None:
    with pytest.raises(ValidationError, match="APP_BASE_URL"):
        api_settings(app_base_url=url)


@pytest.mark.parametrize(
    ("keys", "version"),
    [
        ("not json", 1),
        ('{"1": "not base64!"}', 1),
        (f'{{"1": "{base64.b64encode(b"short").decode()}"}}', 1),
        (FIELD_KEYS, 2),
    ],
)
def test_field_encryption_keys_are_checked(keys: str, version: int) -> None:
    with pytest.raises(ValidationError, match="FIELD_ENCRYPTION"):
        api_settings(field_encryption_keys=keys, field_encryption_active_version=version)


def test_missing_required_settings_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "DATABASE_URL_WORKER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        WorkerSettings()


def test_worker_and_migration_settings_require_tls_when_deployed() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL_WORKER"):
        WorkerSettings.model_validate(
            {"app_env": AppEnv.PRODUCTION, "database_url_worker": LOCAL_DB} | WORKER
        )
    with pytest.raises(ValidationError, match="DATABASE_URL_MIGRATOR"):
        MigrationSettings(app_env=AppEnv.STAGING, database_url_migrator=LOCAL_DB)


def test_secrets_are_not_shown_in_repr() -> None:
    settings = api_settings()
    assert "pw@" not in repr(settings)
    assert HMAC_KEY not in repr(settings)
    assert FIELD_KEY not in repr(settings)


def test_sqlalchemy_url_selects_psycopg() -> None:
    settings = api_settings()
    assert sqlalchemy_url(settings.database_url_app).startswith("postgresql+psycopg://")


def test_worker_refuses_plain_smtp_outside_local_and_test() -> None:
    with pytest.raises(ValidationError, match="SMTP_SECURITY=none"):
        WorkerSettings.model_validate(
            {"app_env": AppEnv.STAGING, "database_url_worker": DEPLOYED_DB}
            | (WORKER | {"smtp_security": "none"})
        )
    WorkerSettings.model_validate(
        {"app_env": AppEnv.LOCAL, "database_url_worker": LOCAL_DB} | (WORKER | {"smtp_security": "none"})
    )
    with pytest.raises(ValidationError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        WorkerSettings.model_validate(
            {"app_env": AppEnv.LOCAL, "database_url_worker": LOCAL_DB} | (WORKER | {"smtp_username": "u"})
        )


def test_email_lookup_key_must_differ_from_the_rate_limit_key() -> None:
    with pytest.raises(ValidationError, match="one key per purpose"):
        api_settings(email_lookup_hmac_key=HMAC_KEY)
    with pytest.raises(ValidationError, match="EMAIL_LOOKUP_HMAC_KEY"):
        api_settings(email_lookup_hmac_key=base64.b64encode(b"short").decode())


def test_blank_smtp_credentials_mean_none() -> None:
    settings = WorkerSettings.model_validate(
        {"app_env": AppEnv.LOCAL, "database_url_worker": LOCAL_DB, "smtp_username": "", "smtp_password": ""}
        | WORKER
    )
    assert settings.smtp_username is None
    assert settings.smtp_password is None


def worker(**overrides: object) -> WorkerSettings:
    return WorkerSettings.model_validate(
        {"app_env": AppEnv.LOCAL, "database_url_worker": LOCAL_DB} | WORKER | overrides
    )


def test_worker_signing_key_must_be_listed_for_verification() -> None:
    assert worker().signature_verifier().matches(worker().checkpoint_signer())
    other = base64.b64encode(
        Ed25519PrivateKey.from_private_bytes(b"o" * 32)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    for keys in (json.dumps({"key-1": other}), json.dumps({"key-2": PUBLIC_KEY})):
        with pytest.raises(ValidationError, match="AUDIT_VERIFY_PUBLIC_KEYS must list"):
            worker(audit_verify_public_keys=keys)
    with pytest.raises(ValidationError, match="AUDIT_SIGNING_PRIVATE_KEY"):
        worker(audit_signing_private_key=base64.b64encode(b"short").decode())
    with pytest.raises(ValidationError, match="AUDIT_VERIFY_PUBLIC_KEYS"):
        worker(audit_verify_public_keys="not json")
    with pytest.raises(ValidationError, match="key ID"):
        worker(audit_signing_key_id="bad id/with spaces")


def test_worker_audit_connections_need_tls_when_deployed() -> None:
    deployed = {"app_env": AppEnv.PRODUCTION, "database_url_worker": DEPLOYED_DB}
    WorkerSettings.model_validate(deployed | WORKER)
    with pytest.raises(ValidationError, match="DATABASE_URL_AUDIT_RETENTION"):
        WorkerSettings.model_validate(deployed | WORKER | {"database_url_audit_retention": LOCAL_DB})
    with pytest.raises(ValidationError, match="S3_ENDPOINT_URL must use https"):
        WorkerSettings.model_validate(deployed | WORKER | {"s3_endpoint_url": "http://s3.internal:9000"})
    assert worker(s3_endpoint_url="http://127.0.0.1:9000").s3_endpoint_url == "http://127.0.0.1:9000"
    assert worker().s3_endpoint_url is None


def test_audit_retention_is_unset_unless_configured() -> None:
    settings = worker(audit_log_retention_months="", security_events_retention_months="24")
    assert settings.audit_log_retention_months is None
    assert settings.security_events_retention_months == 24
    for months in (0, 1201):
        with pytest.raises(ValidationError):
            worker(audit_log_retention_months=months)


def test_file_storage_is_optional_locally_and_required_when_deployed() -> None:
    local = api_settings(files_bucket="", files_access_key_id="", files_secret_access_key="")
    assert local.file_store() is None
    assert api_settings().file_store() is not None
    with pytest.raises(ValidationError, match="FILES_BUCKET is required"):
        api_settings(
            app_env=AppEnv.PRODUCTION,
            database_url_app=DEPLOYED_DB,
            redis_url="rediss://hrms_ratelimit:pw@cache.internal:6379/0",
            app_base_url="https://hrms.example.com",
            files_bucket="",
            files_access_key_id="",
            files_secret_access_key="",
        )
    with pytest.raises(ValidationError, match="set together"):
        api_settings(files_secret_access_key="")
    with pytest.raises(ValidationError, match="S3_REGION"):
        api_settings(s3_region="")


def test_deployed_workers_need_the_virus_scanner() -> None:
    deployed = {"app_env": AppEnv.PRODUCTION, "database_url_worker": DEPLOYED_DB}
    WorkerSettings.model_validate(deployed | WORKER)
    with pytest.raises(ValidationError, match="CLAMAV_HOST"):
        WorkerSettings.model_validate(deployed | WORKER | {"clamav_host": ""})
