"""The settings registry (docs/database-design.md §4.11, docs/security-architecture.md §3.4).

Every setting the running system may change is declared here, once: an explicit name, a
type, a documented default, safe bounds, a unit, and the permission needed to change it.
There are no arbitrary keys: a name not in the registry cannot be read or written.

- Defaults live in code. `app.settings` holds only values someone has changed, so a default
  can never drift between code and database. Removing an override restores the default.
- Secrets are never settings. Keys, passwords and credentials stay in the environment
  (secret manager); a registry entry whose name looks like a secret is refused at import.
- Security floors stay fixed in code, not here: the access-token lifetime, the 10-minute
  step-up window (AUTH-8), the number of recovery codes, the five-factor limit and the sign-in
  challenge lifetime.
- Both change permissions require step-up and are audited (`authorization-model.md` §3.1).
  `security.*` settings need `security.settings.manage`; the rest need `settings.manage`.

The defaults are the values written in the security architecture, except the trusted-device
lifetime, which the architecture leaves open (90 days is the chosen default, adjustable).
"""

import dataclasses
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Final

from app.platform.db import Index, Record, Session, asc, utcnow
from app.platform.tenancy import Organization

logger = logging.getLogger(__name__)


@dataclasses.dataclass(kw_only=True, eq=False)
class SettingOverride(Record):
    """`organization_settings`: one changed value of one organization."""

    __collection__ = "organization_settings"
    __indexes__ = (Index(asc("organization_id", "key"), name="uq_organization_settings_key", unique=True),)
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {"key": {"maxLength": 100}}

    key: str
    value: int
    version: int = 1
    updated_by: uuid.UUID | None = None
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


_SECRET_PARTS: Final = frozenset(
    {"secret", "key", "keys", "token", "credential", "credentials", "pepper", "passphrase", "dsn", "url"}
)


def looks_like_secret(key: str) -> bool:
    """A name that would hold a secret: a part naming one, or a name ending in "password"."""
    parts = key.replace(".", "_").split("_")
    return not _SECRET_PARTS.isdisjoint(parts) or parts[-1] == "password"


class Unit(StrEnum):
    ATTEMPTS = "attempts"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"


@dataclass(frozen=True, slots=True)
class IntSetting:
    key: str
    description: str
    default: int
    minimum: int
    maximum: int
    unit: Unit

    def __post_init__(self) -> None:
        if looks_like_secret(self.key):
            raise ValueError(f"{self.key!r} looks like a secret; secrets belong in the environment")
        if not self.minimum <= self.default <= self.maximum:
            raise ValueError(f"the default of {self.key} is outside its bounds")

    @property
    def permission(self) -> str:
        return "security.settings.manage" if self.key.startswith("security.") else "settings.manage"

    def check(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingValueError(self, "must be a whole number")
        if not self.minimum <= value <= self.maximum:
            raise SettingValueError(self, f"must be between {self.minimum} and {self.maximum} {self.unit}")
        return value


class SettingValueError(ValueError):
    def __init__(self, setting: IntSetting, message: str) -> None:
        super().__init__(f"{setting.key} {message}")
        self.setting = setting
        self.message = message


LOCKOUT_THRESHOLD: Final = IntSetting(
    "security.lockout.threshold",
    "Failed sign-in attempts within the window that lock an account",
    10,
    3,
    20,
    Unit.ATTEMPTS,
)
LOCKOUT_DELAY_AFTER: Final = IntSetting(
    "security.lockout.delay_after",
    "Failed attempts within the window after which each further attempt is slowed down",
    5,
    1,
    19,
    Unit.ATTEMPTS,
)
LOCKOUT_WINDOW: Final = IntSetting(
    "security.lockout.window_minutes", "How long failed attempts keep counting", 15, 5, 60, Unit.MINUTES
)
LOCKOUT_DURATION: Final = IntSetting(
    "security.lockout.duration_minutes", "How long a locked account stays locked", 15, 5, 120, Unit.MINUTES
)
LOGIN_IP_FAILURES: Final = IntSetting(
    "security.login.ip_failure_threshold",
    "Failed sign-in attempts from one network address within 5 minutes that block it for 15 minutes",
    20,
    5,
    200,
    Unit.ATTEMPTS,
)
SESSION_IDLE: Final = IntSetting(
    "security.session.idle_timeout_minutes",
    "Inactivity after which a session ends (applies to new sessions)",
    30,
    5,
    240,
    Unit.MINUTES,
)
SESSION_ABSOLUTE: Final = IntSetting(
    "security.session.absolute_timeout_hours",
    "Maximum length of a session however active (applies to new sessions)",
    12,
    1,
    24,
    Unit.HOURS,
)
INVITE_TTL: Final = IntSetting(
    "security.invite.ttl_hours", "How long an invite link works", 72, 1, 168, Unit.HOURS
)
PASSWORD_RESET_TTL: Final = IntSetting(
    "security.password_reset.ttl_minutes", "How long a password reset link works", 30, 10, 60, Unit.MINUTES
)
MFA_REENROLMENT_TTL: Final = IntSetting(
    "security.mfa_reenrolment.ttl_hours",
    "How long the re-enrolment link sent after an MFA reset works",
    24,
    1,
    72,
    Unit.HOURS,
)
TRUSTED_DEVICE_LIFETIME: Final = IntSetting(
    "security.trusted_device.lifetime_days",
    "How long a browser stays recognized before a sign-in from it is reported as new again",
    90,
    1,
    365,
    Unit.DAYS,
)
EMAIL_MAX_ATTEMPTS: Final = IntSetting(
    "email.delivery.max_attempts",
    "Delivery attempts for an email before it is marked failed",
    5,
    1,
    10,
    Unit.ATTEMPTS,
)

REGISTRY: Final[dict[str, IntSetting]] = {
    setting.key: setting
    for setting in (
        LOCKOUT_THRESHOLD,
        LOCKOUT_DELAY_AFTER,
        LOCKOUT_WINDOW,
        LOCKOUT_DURATION,
        LOGIN_IP_FAILURES,
        SESSION_IDLE,
        SESSION_ABSOLUTE,
        INVITE_TTL,
        PASSWORD_RESET_TTL,
        MFA_REENROLMENT_TTL,
        TRUSTED_DEVICE_LIFETIME,
        EMAIL_MAX_ATTEMPTS,
    )
}


def check_combination(values: Mapping[str, int]) -> None:
    """Rules between settings, checked on every change against the effective values."""
    if values[LOCKOUT_DELAY_AFTER.key] >= values[LOCKOUT_THRESHOLD.key]:
        raise SettingValueError(LOCKOUT_DELAY_AFTER, "must be lower than the lockout threshold")
    if values[SESSION_IDLE.key] > values[SESSION_ABSOLUTE.key] * 60:
        raise SettingValueError(SESSION_IDLE, "cannot be longer than the absolute session timeout")


@dataclass(frozen=True, slots=True)
class Settings:
    """The effective values: overrides where set, defaults otherwise."""

    values: Mapping[str, int]
    overridden: frozenset[str]

    def __getitem__(self, setting: IntSetting) -> int:
        return self.values[setting.key]


async def load(session: Session) -> Settings:
    """The effective values of the session's organization; the defaults when the request has no
    organization yet (a sign-in attempt for an unknown email)."""
    values = {key: setting.default for key, setting in REGISTRY.items()}
    overridden: set[str] = set()
    if session.tenant is None:
        return Settings(values, frozenset())
    for row in await session.find(SettingOverride):
        setting = REGISTRY.get(row.key)
        if setting is None:
            # A key removed from the registry: ignored, never applied.
            logger.warning("settings.unknown_key_ignored", extra={"setting": row.key})
            continue
        try:
            values[row.key] = setting.check(row.value)
        except SettingValueError:
            logger.error("settings.invalid_value_ignored", extra={"setting": row.key})
            continue
        overridden.add(row.key)
    return Settings(values, frozenset(overridden))


async def store(session: Session, setting: IntSetting, value: int | None, user_id: uuid.UUID | None) -> None:
    """Write an override (or remove it with None). The caller has authorized and validated it,
    and holds the settings lock (`lock`)."""
    existing = await session.find_one(SettingOverride, {"key": setting.key})
    if value is None:
        if existing is not None:
            await session.delete(existing)
        return
    if existing is None:
        session.add(SettingOverride(key=setting.key, value=value, updated_by=user_id))
        return
    existing.value = value
    existing.updated_by = user_id
    existing.updated_at = utcnow()
    existing.version += 1


async def lock(session: Session) -> None:
    """One settings change per organization at a time, so the combined rules hold: changes
    lock the organization's document."""
    if session.tenant is None or await session.get(Organization, session.tenant, for_update=True) is None:
        raise LookupError("settings are changed inside an existing organization")
