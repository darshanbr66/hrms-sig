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

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Table, Text, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.db import Base

logger = logging.getLogger(__name__)

SCHEMA = "app"

settings_table = Table(
    "settings",
    Base.metadata,
    Column("key", Text(), primary_key=True),
    Column("value", JSONB(), nullable=False),
    Column("version", Integer(), nullable=False, server_default=text("1")),
    Column("updated_by", ForeignKey("identity.users.id", ondelete="RESTRICT")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)


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


async def load(session: AsyncSession) -> Settings:
    rows = (await session.execute(select(settings_table.c.key, settings_table.c.value))).all()
    values = {key: setting.default for key, setting in REGISTRY.items()}
    overridden: set[str] = set()
    for key, value in rows:
        setting = REGISTRY.get(key)
        if setting is None:
            # A key removed from the registry: ignored, never applied.
            logger.warning("settings.unknown_key_ignored", extra={"setting": key})
            continue
        try:
            values[key] = setting.check(value)
        except SettingValueError:
            logger.error("settings.invalid_value_ignored", extra={"setting": key})
            continue
        overridden.add(key)
    return Settings(values, frozenset(overridden))


async def store(session: AsyncSession, setting: IntSetting, value: int | None, user_id: Any) -> None:
    """Write an override (or remove it with None). The caller has authorized and validated it,
    and holds the settings lock (`lock`)."""
    if value is None:
        await session.execute(settings_table.delete().where(settings_table.c.key == setting.key))
        return
    statement = insert(settings_table).values(key=setting.key, value=value, updated_by=user_id)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[settings_table.c.key],
            set_={
                "value": statement.excluded.value,
                "updated_by": statement.excluded.updated_by,
                "updated_at": func.now(),
                "version": settings_table.c.version + 1,
            },
        )
    )


SETTINGS_LOCK: Final = 0x5356_5354  # "SVST": one settings change at a time, so combined rules hold


async def lock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETTINGS_LOCK})
