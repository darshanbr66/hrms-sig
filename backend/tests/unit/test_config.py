import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import ValidationError

from app.platform.config import (
    ApiSettings,
    AppEnv,
    EmailTransport,
    JobsInApiSettings,
    MigrationSettings,
    WorkerSettings,
)

HMAC_KEY = base64.b64encode(b"k" * 32).decode()
EMAIL_KEY = base64.b64encode(b"m" * 32).decode()
LINK_KEY = base64.b64encode(b"l" * 32).decode()
FIELD_KEY = base64.b64encode(b"f" * 32).decode()
FIELD_KEYS = f'{{"1": "{FIELD_KEY}"}}'
LOCAL_DB = "mongodb://hrms_app:pw@127.0.0.1:27017/?directConnection=true"
DEPLOYED_DB = "mongodb+srv://hrms_app:pw@cluster0.example.mongodb.net/?retryWrites=true&w=majority"
SIGNING_SEED = b"s" * 32
PUBLIC_KEY = base64.b64encode(
    Ed25519PrivateKey.from_private_bytes(SIGNING_SEED)
    .public_key()
    .public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode()
# Everything a worker needs besides its environment and database URI.
WORKER: dict[str, object] = {
    "app_base_url": "https://hrms.example.com",
    "mongodb_database": "hrms",
    "email_from": "Sigvitas HRMS <hrms@example.com>",
    "resend_api_key": "re_test_not_a_real_key",
    "audit_signing_key_id": "key-1",
    "audit_signing_private_key": base64.b64encode(SIGNING_SEED).decode(),
    "audit_verify_public_keys": json.dumps({"key-1": PUBLIC_KEY}),
}
# Production workers need the audit witness's recipients.
WITNESS: dict[str, object] = {"audit_witness_emails": "auditor@example.org"}
PRODUCTION_WORKER: dict[str, object] = (
    WORKER | WITNESS | {"app_env": AppEnv.PRODUCTION, "mongodb_uri": DEPLOYED_DB}
)
RETENTION_DB = "mongodb+srv://hrms_audit_retention:pw@cluster0.example.mongodb.net/?retryWrites=true"
WORKER_DB = "mongodb+srv://hrms_worker:pw@cluster0.example.mongodb.net/?retryWrites=true"


def api_settings(**overrides: object) -> ApiSettings:
    values: dict[str, object] = {
        "app_env": AppEnv.LOCAL,
        "mongodb_uri": LOCAL_DB,
        "mongodb_database": "hrms_local",
        "rate_limit_key_hmac_key": HMAC_KEY,
        "email_lookup_hmac_key": EMAIL_KEY,
        "download_link_hmac_key": LINK_KEY,
        "app_base_url": "http://localhost:5173",
        "field_encryption_keys": FIELD_KEYS,
        "field_encryption_active_version": 1,
    }
    values.update(overrides)
    return ApiSettings.model_validate(values)


LOCAL: dict[str, object] = {
    "app_env": AppEnv.LOCAL,
    "mongodb_uri": LOCAL_DB,
    "mongodb_database": "hrms_local",
}
# The local mail catcher.
SMTP: dict[str, object] = {"email_transport": "smtp", "smtp_host": "127.0.0.1", "smtp_port": 1025}


def worker(**overrides: object) -> WorkerSettings:
    return WorkerSettings.model_validate(WORKER | LOCAL | overrides)


def test_local_settings_are_accepted() -> None:
    settings = api_settings(trusted_proxy_cidrs="10.0.0.0/8, 192.168.1.0/24")
    assert [str(network) for network in settings.trusted_proxy_cidrs] == ["10.0.0.0/8", "192.168.1.0/24"]
    assert settings.rate_limit_hmac_key_bytes == b"k" * 32
    assert settings.mongodb_database == "hrms_local"
    # Uploads stay off unless turned on (no scanner is assumed).
    assert settings.document_uploads_enabled is False


def test_production_settings_with_tls_are_accepted() -> None:
    settings = api_settings(
        app_env=AppEnv.PRODUCTION, mongodb_uri=DEPLOYED_DB, app_base_url="https://hrms.example.com/"
    )
    assert settings.app_env.is_deployed
    assert settings.app_base_url == "https://hrms.example.com"
    tls = "mongodb://hrms_app:pw@db.internal:27017/?tls=true&replicaSet=rs0"
    assert api_settings(app_env=AppEnv.PRODUCTION, mongodb_uri=tls, app_base_url="https://hrms.example.com")


@pytest.mark.parametrize("app_env", [AppEnv.STAGING, AppEnv.PRODUCTION])
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"mongodb_uri": LOCAL_DB}, "TLS"),
        ({"mongodb_uri": DEPLOYED_DB + "&tls=false"}, "tls=false"),
        ({"mongodb_uri": DEPLOYED_DB + "&tlsAllowInvalidCertificates=true"}, "tlsallowinvalidcertificates"),
        ({"mongodb_uri": DEPLOYED_DB + "&tlsInsecure=true"}, "tlsinsecure"),
        ({"mongodb_uri": "mongodb+srv://cluster0.example.mongodb.net/"}, "database user"),
        ({"mongodb_uri": "postgresql://hrms_app:pw@db.internal/hrms"}, "mongodb://"),
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
        "mongodb_uri": DEPLOYED_DB,
        "app_base_url": "https://hrms.example.com",
    }
    with pytest.raises(ValidationError, match=message):
        api_settings(**(safe | overrides))


@pytest.mark.parametrize("key", ["not base64!", base64.b64encode(b"short").decode()])
def test_weak_or_malformed_hmac_key_is_refused(key: str) -> None:
    with pytest.raises(ValidationError, match="RATE_LIMIT_KEY_HMAC_KEY"):
        api_settings(rate_limit_key_hmac_key=key)
    with pytest.raises(ValidationError, match="DOWNLOAD_LINK_HMAC_KEY"):
        api_settings(download_link_hmac_key=key)


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
    for name in ("APP_ENV", "MONGODB_URI"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        WorkerSettings()


def test_worker_and_migration_settings_require_tls_when_deployed() -> None:
    with pytest.raises(ValidationError, match="MONGODB_URI"):
        WorkerSettings.model_validate({"app_env": AppEnv.PRODUCTION, "mongodb_uri": LOCAL_DB} | WORKER)
    with pytest.raises(ValidationError, match="MONGODB_URI"):
        MigrationSettings(app_env=AppEnv.STAGING, mongodb_uri=LOCAL_DB, mongodb_database="hrms")
    with pytest.raises(ValidationError, match="mongodb_database"):
        MigrationSettings(app_env=AppEnv.LOCAL, mongodb_uri=LOCAL_DB, mongodb_database="bad name/")


def test_secrets_are_not_shown_in_repr() -> None:
    settings = api_settings()
    assert "pw@" not in repr(settings)
    assert HMAC_KEY not in repr(settings)
    assert FIELD_KEY not in repr(settings)
    assert LINK_KEY not in repr(settings)


def test_refused_settings_do_not_print_their_values() -> None:
    """A startup error names the setting and the rule, never the value: it goes to the logs."""
    short_key = base64.b64encode(b"leaked-key-material").decode()
    with pytest.raises(ValidationError) as field_error:
        api_settings(rate_limit_key_hmac_key=short_key)
    with pytest.raises(ValidationError) as model_error:
        MigrationSettings(
            app_env=AppEnv.STAGING,
            mongodb_uri="mongodb://m:db-password-1@db.internal/hrms",
            mongodb_database="hrms",
        )
    for error in (field_error.value, model_error.value):
        text = str(error)
        assert "input_value" not in text
        assert short_key not in text
        assert "db-password-1" not in text
    assert "RATE_LIMIT_KEY_HMAC_KEY" in str(field_error.value)
    assert "MONGODB_URI" in str(model_error.value)


def test_email_goes_through_resend_unless_local_smtp_is_chosen() -> None:
    assert worker().email_transport is EmailTransport.RESEND
    with pytest.raises(ValidationError, match="RESEND_API_KEY is required"):
        worker(resend_api_key="")
    local = worker(**SMTP, resend_api_key="")
    assert local.email_transport is EmailTransport.SMTP
    with pytest.raises(ValidationError, match="SMTP_HOST is required"):
        worker(**(SMTP | {"smtp_host": ""}))
    with pytest.raises(ValidationError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        worker(**SMTP, smtp_username="u")
    for app_env, name in ((AppEnv.STAGING, "hrms_staging"), (AppEnv.PRODUCTION, "hrms")):
        with pytest.raises(ValidationError, match="EMAIL_TRANSPORT=smtp is allowed only in local and test"):
            WorkerSettings.model_validate(
                WORKER
                | SMTP
                | WITNESS
                | {
                    "app_env": app_env,
                    "mongodb_uri": DEPLOYED_DB,
                    "mongodb_database": name,
                    "smtp_security": "tls",
                }
            )


@pytest.mark.parametrize("sender", ["", "no-address", "@example.com", "HRMS <hrms@example.com>\nBcc: x@y.z"])
def test_email_from_must_be_one_address(sender: str) -> None:
    with pytest.raises(ValidationError, match="EMAIL_FROM"):
        worker(email_from=sender)


def test_resend_key_is_not_shown_in_repr() -> None:
    assert "re_test_not_a_real_key" not in repr(worker())


def test_audit_witness_mailboxes_are_a_list_of_addresses() -> None:
    assert worker().audit_witness_emails == ()
    settings = worker(audit_witness_emails="auditor@example.org, second@example.net")
    assert settings.audit_witness_emails == ("auditor@example.org", "second@example.net")
    with pytest.raises(ValidationError, match="audit_witness_emails"):
        worker(audit_witness_emails="not an address")


@pytest.mark.parametrize("name", ["hrms", "HRMS"])
def test_only_production_uses_the_production_database(name: str) -> None:
    for app_env in (AppEnv.LOCAL, AppEnv.TEST):
        with pytest.raises(ValidationError, match="production database"):
            MigrationSettings(app_env=app_env, mongodb_uri=LOCAL_DB, mongodb_database=name)
        with pytest.raises(ValidationError, match="production database"):
            api_settings(app_env=app_env, mongodb_database=name)
    staging = {
        "app_env": AppEnv.STAGING,
        "mongodb_uri": DEPLOYED_DB,
        "app_base_url": "https://hrms.example.com",
    }
    with pytest.raises(ValidationError, match="production database"):
        MigrationSettings(app_env=AppEnv.STAGING, mongodb_uri=DEPLOYED_DB, mongodb_database=name)
    with pytest.raises(ValidationError, match="production database"):
        api_settings(**staging, mongodb_database=name)
    assert api_settings(**staging, mongodb_database="hrms_staging")
    assert MigrationSettings(app_env=AppEnv.PRODUCTION, mongodb_uri=DEPLOYED_DB, mongodb_database="hrms")


def test_the_database_name_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)
    with pytest.raises(ValidationError, match="mongodb_database"):
        MigrationSettings(app_env=AppEnv.LOCAL, mongodb_uri=LOCAL_DB)


def test_jobs_in_the_api_keep_the_worker_database_user() -> None:
    local = JobsInApiSettings.model_validate(WORKER | LOCAL)
    assert local.jobs_mongodb_uri.get_secret_value() == LOCAL_DB
    worker_uri = WORKER_DB
    deployed = PRODUCTION_WORKER
    with pytest.raises(ValidationError, match="MONGODB_WORKER_URI is required"):
        JobsInApiSettings.model_validate(deployed)
    with pytest.raises(ValidationError, match="MONGODB_WORKER_URI must use TLS"):
        JobsInApiSettings.model_validate(deployed | {"mongodb_worker_uri": LOCAL_DB})
    settings = JobsInApiSettings.model_validate(deployed | {"mongodb_worker_uri": worker_uri})
    assert settings.jobs_mongodb_uri.get_secret_value() == worker_uri
    assert api_settings(run_jobs_in_api="true").run_jobs_in_api is True


def test_production_jobs_in_the_api_may_share_the_api_database_user() -> None:
    same_uri = JobsInApiSettings.model_validate(PRODUCTION_WORKER | {"mongodb_worker_uri": DEPLOYED_DB})
    assert same_uri.app_env is AppEnv.PRODUCTION
    assert same_uri.jobs_mongodb_uri.get_secret_value() == DEPLOYED_DB
    # The same user written another way (other host, options) is accepted too.
    same_user = "mongodb://hrms_app:other@db-0.example.net:27017,db-1.example.net:27017/?tls=true"
    settings = JobsInApiSettings.model_validate(PRODUCTION_WORKER | {"mongodb_worker_uri": same_user})
    assert settings.jobs_mongodb_uri.get_secret_value() == same_user
    # Sharing the user does not relax the other rules on the worker URI.
    with pytest.raises(ValidationError, match="MONGODB_WORKER_URI must use TLS"):
        JobsInApiSettings.model_validate(PRODUCTION_WORKER | {"mongodb_worker_uri": LOCAL_DB})


def test_hmac_keys_must_differ_from_each_other() -> None:
    with pytest.raises(ValidationError, match="one key per purpose"):
        api_settings(email_lookup_hmac_key=HMAC_KEY)
    with pytest.raises(ValidationError, match="one key per purpose"):
        api_settings(download_link_hmac_key=EMAIL_KEY)
    with pytest.raises(ValidationError, match="EMAIL_LOOKUP_HMAC_KEY"):
        api_settings(email_lookup_hmac_key=base64.b64encode(b"short").decode())


def test_blank_smtp_credentials_mean_none() -> None:
    settings = worker(**SMTP, smtp_username="", smtp_password="")
    assert settings.smtp_username is None
    assert settings.smtp_password is None


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


def test_audit_retention_needs_its_own_database_user() -> None:
    settings = worker(audit_log_retention_months="", security_events_retention_months="")
    assert settings.audit_log_retention_months is None
    assert settings.mongodb_audit_retention_uri is None
    with pytest.raises(ValidationError, match="MONGODB_AUDIT_RETENTION_URI is required"):
        worker(security_events_retention_months="24")
    retention = "mongodb://hrms_audit_retention:pw@127.0.0.1:27017/?directConnection=true"
    settings = worker(security_events_retention_months="24", mongodb_audit_retention_uri=retention)
    assert settings.security_events_retention_months == 24
    for months in (0, 1201):
        with pytest.raises(ValidationError):
            worker(audit_log_retention_months=months, mongodb_audit_retention_uri=retention)
    deployed = {"app_env": AppEnv.PRODUCTION, "mongodb_uri": DEPLOYED_DB}
    with pytest.raises(ValidationError, match="MONGODB_AUDIT_RETENTION_URI"):
        WorkerSettings.model_validate(
            deployed | WORKER | {"audit_log_retention_months": 24, "mongodb_audit_retention_uri": retention}
        )


def test_deployed_workers_need_the_virus_scanner_only_while_uploads_are_on() -> None:
    without_scanner = WorkerSettings.model_validate(PRODUCTION_WORKER)
    assert without_scanner.clamav_host is None
    assert without_scanner.document_uploads_enabled is False
    with pytest.raises(ValidationError, match="DOCUMENT_UPLOADS_ENABLED=false"):
        WorkerSettings.model_validate(PRODUCTION_WORKER | {"document_uploads_enabled": "true"})
    WorkerSettings.model_validate(
        PRODUCTION_WORKER | {"document_uploads_enabled": "true", "clamav_host": "clamav"}
    )
    assert api_settings(document_uploads_enabled="true").document_uploads_enabled is True


@pytest.mark.parametrize("settings_class", [WorkerSettings, JobsInApiSettings])
def test_production_jobs_need_the_audit_witness(settings_class: type[WorkerSettings]) -> None:
    jobs_user = {"mongodb_worker_uri": WORKER_DB}
    without = PRODUCTION_WORKER | jobs_user | {"audit_witness_emails": ""}
    with pytest.raises(ValidationError, match="AUDIT_WITNESS_EMAILS is required in production"):
        settings_class.model_validate(without)
    with pytest.raises(ValidationError, match="AUDIT_WITNESS_EMAILS is required in production"):
        settings_class.model_validate({k: v for k, v in without.items() if k != "audit_witness_emails"})
    with pytest.raises(ValidationError, match="audit_witness_emails"):
        settings_class.model_validate(without | {"audit_witness_emails": "not an address"})
    settings = settings_class.model_validate(
        without | {"audit_witness_emails": "auditor@example.org, records@auditors.example.net"}
    )
    assert settings.audit_witness_emails == ("auditor@example.org", "records@auditors.example.net")
    # Staging and local runs may go without one (a daily warning says the trail is not witnessed).
    staging = without | {"app_env": AppEnv.STAGING, "mongodb_database": "hrms_staging"}
    assert settings_class.model_validate(staging).audit_witness_emails == ()
    assert worker().audit_witness_emails == ()


@pytest.mark.parametrize(
    "retention",
    [
        {"mongodb_audit_retention_uri": RETENTION_DB},
        {"mongodb_audit_retention_uri": RETENTION_DB, "audit_log_retention_months": 24},
        {"security_events_retention_months": 24},
    ],
)
@pytest.mark.parametrize("app_env", [AppEnv.STAGING, AppEnv.PRODUCTION])
def test_audit_retention_never_runs_inside_a_deployed_api(
    app_env: AppEnv, retention: dict[str, object]
) -> None:
    deployed = PRODUCTION_WORKER | {
        "app_env": app_env,
        "mongodb_database": "hrms" if app_env is AppEnv.PRODUCTION else "hrms_staging",
        "mongodb_worker_uri": WORKER_DB,
    }
    with pytest.raises(ValidationError, match="audit retention does not run inside the API"):
        JobsInApiSettings.model_validate(deployed | retention)
    # Retention off: the jobs run inside the API, and no retention user is loaded.
    inside = JobsInApiSettings.model_validate(deployed | {"audit_log_retention_months": ""})
    assert inside.mongodb_audit_retention_uri is None
    assert not inside.retention_configured


def test_a_separate_worker_may_run_audit_retention_as_its_own_user() -> None:
    separate = WorkerSettings.model_validate(
        PRODUCTION_WORKER | {"mongodb_audit_retention_uri": RETENTION_DB, "audit_log_retention_months": 24}
    )
    assert separate.retention_configured
    assert separate.audit_log_retention_months == 24
    same_user = "mongodb+srv://hrms_app:pw@other.example.mongodb.net/?retryWrites=true"
    with pytest.raises(ValidationError, match="MONGODB_AUDIT_RETENTION_URI must name another database user"):
        WorkerSettings.model_validate(
            PRODUCTION_WORKER | {"mongodb_audit_retention_uri": same_user, "audit_log_retention_months": 24}
        )
    # Local runs keep retention inside the API for the integration tests.
    retention = "mongodb://hrms_audit_retention:pw@127.0.0.1:27017/?directConnection=true"
    local = JobsInApiSettings.model_validate(
        WORKER | LOCAL | {"mongodb_audit_retention_uri": retention, "audit_log_retention_months": 24}
    )
    assert local.retention_configured
