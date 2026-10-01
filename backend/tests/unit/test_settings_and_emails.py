"""The settings registry and canonical, keyed email hashing (no database)."""

import hashlib
import hmac

import pytest

from app.modules.identity.notifications import browser_label
from app.platform import settings
from app.platform.security.emails import EmailHasher, canonical_email

# --- settings registry -------------------------------------------------------------------

# The documented security defaults (docs/security-architecture.md §3, product requirements).
DOCUMENTED = {
    "security.lockout.threshold": 10,
    "security.lockout.delay_after": 5,
    "security.lockout.window_minutes": 15,
    "security.lockout.duration_minutes": 15,
    "security.login.ip_failure_threshold": 20,
    "security.session.idle_timeout_minutes": 30,
    "security.session.absolute_timeout_hours": 12,
    "security.invite.ttl_hours": 72,
    "security.password_reset.ttl_minutes": 30,
    "email.delivery.max_attempts": 5,
}


def test_defaults_are_the_documented_values() -> None:
    for key, value in DOCUMENTED.items():
        assert settings.REGISTRY[key].default == value, key
    assert settings.TRUSTED_DEVICE_LIFETIME.default == 90


def test_defaults_satisfy_the_rules_between_settings() -> None:
    settings.check_combination({key: setting.default for key, setting in settings.REGISTRY.items()})


def test_security_settings_need_the_security_permission() -> None:
    for setting in settings.REGISTRY.values():
        expected = "security.settings.manage" if setting.key.startswith("security.") else "settings.manage"
        assert setting.permission == expected


@pytest.mark.parametrize("value", [True, 1.5, "10", None, -1, 21, 2])
def test_values_are_whole_numbers_within_bounds(value: object) -> None:
    with pytest.raises(settings.SettingValueError):
        settings.LOCKOUT_THRESHOLD.check(value)
    assert settings.LOCKOUT_THRESHOLD.check(12) == 12


@pytest.mark.parametrize(
    "key",
    [
        "smtp.password",
        "email.signing.key",
        "security.api.token",
        "storage.credentials.user",
        "database.url",
        "audit.signing_secret.value",
    ],
)
def test_secrets_cannot_become_settings(key: str) -> None:
    assert settings.looks_like_secret(key)
    with pytest.raises(ValueError, match="looks like a secret"):
        settings.IntSetting(key, "x", 1, 0, 2, settings.Unit.ATTEMPTS)


def test_ordinary_names_are_not_secret_shaped() -> None:
    for key in settings.REGISTRY:
        assert not settings.looks_like_secret(key), key


def test_rules_between_settings() -> None:
    values = {key: setting.default for key, setting in settings.REGISTRY.items()}
    with pytest.raises(settings.SettingValueError, match="lower than the lockout threshold"):
        settings.check_combination(values | {"security.lockout.delay_after": 10})
    with pytest.raises(settings.SettingValueError, match="absolute session timeout"):
        settings.check_combination(
            values
            | {"security.session.absolute_timeout_hours": 1, "security.session.idle_timeout_minutes": 61}
        )


# --- canonical email and keyed hash --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("  Person@Dev.Example ", "person@dev.example"),
        ("PERSON@DEV.EXAMPLE", "person@dev.example"),
        ("ｐerson@dev.example", "person@dev.example"),  # noqa: RUF001 (full-width letter, normalized by NFKC)
    ],
)
def test_canonical_email(raw: str, canonical: str) -> None:
    assert canonical_email(raw) == canonical


def test_keyed_hash_is_stable_keyed_and_not_plain_sha256() -> None:
    key = b"k" * 32
    hasher = EmailHasher(key)
    digest = hasher.digest(" Person@Dev.Example")
    assert digest == hasher.digest("person@dev.example")
    assert len(digest) == 32
    assert digest == hmac.new(key, b"person@dev.example", hashlib.sha256).digest()
    # Without the key, a list of candidate addresses does not reverse it.
    assert digest != hashlib.sha256(b"person@dev.example").digest()
    assert digest != EmailHasher(b"j" * 32).digest("person@dev.example")


def test_hash_key_must_be_long_enough() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        EmailHasher(b"short")


@pytest.mark.parametrize(
    ("user_agent", "label"),
    [
        (None, "an unknown browser"),
        ("Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0", "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0"),
        ("Visit https://evil.example/reset now", "Visit https /evil.example/reset now"),
        ("x@evil.example" + chr(13) + chr(10) + "Bcc: victim", "x evil.example Bcc victim"),
        ("@@@", "an unknown browser"),
    ],
)
def test_the_browser_label_in_emails_is_inert(user_agent: str | None, label: str) -> None:
    """The user agent is attacker-chosen: no link, address or line break survives."""
    assert browser_label(user_agent) == label
