import base64

import pytest
from pydantic import ValidationError

from app.platform.config import ApiSettings, AppEnv, MigrationSettings, WorkerSettings, sqlalchemy_url

HMAC_KEY = base64.b64encode(b"k" * 32).decode()
LOCAL_DB = "postgresql://hrms_app:pw@127.0.0.1:5433/hrms"
DEPLOYED_DB = "postgresql://hrms_app:pw@db.internal:5432/hrms?sslmode=verify-full"


def api_settings(**overrides: object) -> ApiSettings:
    values: dict[str, object] = {
        "app_env": AppEnv.LOCAL,
        "database_url_app": LOCAL_DB,
        "redis_url": "redis://hrms_ratelimit:pw@127.0.0.1:6380/0",
        "rate_limit_key_hmac_key": HMAC_KEY,
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
    )
    assert settings.app_env.is_deployed


@pytest.mark.parametrize("app_env", [AppEnv.STAGING, AppEnv.PRODUCTION])
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"database_url_app": LOCAL_DB}, "sslmode=verify-full"),
        ({"database_url_app": DEPLOYED_DB.replace("verify-full", "require")}, "sslmode=verify-full"),
        ({"redis_url": "redis://hrms_ratelimit:pw@cache.internal:6379/0"}, "rediss://"),
        ({"api_docs_enabled": True}, "API_DOCS_ENABLED"),
        ({"log_level": "DEBUG"}, "DEBUG"),
    ],
)
def test_deployed_environments_refuse_unsafe_settings(
    app_env: AppEnv, overrides: dict[str, object], message: str
) -> None:
    safe: dict[str, object] = {
        "app_env": app_env,
        "database_url_app": DEPLOYED_DB,
        "redis_url": "rediss://hrms_ratelimit:pw@cache.internal:6379/0",
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


def test_missing_required_settings_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "DATABASE_URL_WORKER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        WorkerSettings()


def test_worker_and_migration_settings_require_tls_when_deployed() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL_WORKER"):
        WorkerSettings(app_env=AppEnv.PRODUCTION, database_url_worker=LOCAL_DB)
    with pytest.raises(ValidationError, match="DATABASE_URL_MIGRATOR"):
        MigrationSettings(app_env=AppEnv.STAGING, database_url_migrator=LOCAL_DB)


def test_secrets_are_not_shown_in_repr() -> None:
    settings = api_settings()
    assert "pw@" not in repr(settings)
    assert HMAC_KEY not in repr(settings)


def test_sqlalchemy_url_selects_psycopg() -> None:
    settings = api_settings()
    assert sqlalchemy_url(settings.database_url_app).startswith("postgresql+psycopg://")
