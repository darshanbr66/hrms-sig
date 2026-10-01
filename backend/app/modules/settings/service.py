"""Reading and changing settings (docs/database-design.md §4.11, authorization-model.md §3.1).

- Reading needs `settings.read`. Changing needs the setting's own permission:
  `security.settings.manage` for `security.*` keys, `settings.manage` for the others. Both are
  step-up permissions, enforced by the engine, and every change is audited with its old and
  new value (settings hold no personal data).
- Only registry keys exist; anything else is 404, including any attempt to store a secret.
- Changes are serialized (advisory lock) and checked against the rules between settings on
  the effective values, so two concurrent changes cannot together break a rule.
- `None` removes an override, which restores the default.
"""

from dataclasses import dataclass

from app.platform import settings as registry
from app.platform.audit.records import AuditActor, AuditEvent, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType


@dataclass(frozen=True, slots=True)
class SettingState:
    setting: registry.IntSetting
    value: int
    overridden: bool


class SettingsService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    async def list(self) -> list[SettingState]:
        async with self._db.unit_of_work() as session:
            values = await registry.load(session)
        return [
            SettingState(setting, values[setting], setting.key in values.overridden)
            for setting in registry.REGISTRY.values()
        ]

    async def change(self, actor: Actor, context: RequestContext, key: str, value: object) -> SettingState:
        setting = registry.REGISTRY.get(key)
        if setting is None:
            raise ProblemError(ProblemType.NOT_FOUND)
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, actor, setting.permission)
            await registry.lock(session)
            current = await registry.load(session)
            new_value = setting.default if value is None else _checked(setting, value)
            combined = dict(current.values) | {setting.key: new_value}
            try:
                registry.check_combination(combined)
            except registry.SettingValueError as exc:
                raise _invalid(exc) from exc
            old_value = current[setting]
            await registry.store(session, setting, None if value is None else new_value, actor.user_id)
            await self._audit.record(
                session,
                AuditEvent(
                    action="settings.value.changed",
                    actor=AuditActor.user(actor.user_id, session_id=actor.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="setting",
                    target_id=setting.key,
                    changes={"value": ValueChange(old_value, new_value)},
                ),
            )
        return SettingState(setting, new_value, value is not None)


def _checked(setting: registry.IntSetting, value: object) -> int:
    try:
        return setting.check(value)
    except registry.SettingValueError as exc:
        raise _invalid(exc) from exc


def _invalid(exc: registry.SettingValueError) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={
            "errors": [{"field": "value", "code": f"setting.invalid.{exc.setting.key}", "message": str(exc)}]
        },
    )
