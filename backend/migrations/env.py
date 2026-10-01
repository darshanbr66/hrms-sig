"""Alembic environment (docs/database-design.md §11).

- Connects only as hrms_migrator, so every object is owned by the role whose default
  privileges grant the runtime roles their access.
- Sets lock_timeout for the whole run, so a blocked migration fails fast instead of queueing
  application traffic behind its lock.
- One transaction per revision.
- Refuses downgrades in staging and production: a faulty migration is fixed forward.
"""

from collections.abc import Callable, Collection, Mapping
from typing import Any

from alembic import context
from alembic.runtime.migration import MigrationContext, MigrationInfo
from sqlalchemy import create_engine, pool, text

from app.metadata import metadata
from app.platform.config import AppEnv, MigrationSettings, sqlalchemy_url
from app.platform.logging import configure_logging

MIGRATOR_ROLE = "hrms_migrator"
# A blocked migration fails fast instead of queueing application traffic behind its lock.
SET_LOCK_TIMEOUT = "SET lock_timeout = '5s'"

config = context.config


class DowngradeNotAllowedError(RuntimeError):
    pass


def _settings() -> MigrationSettings:
    provided = config.attributes.get("settings")
    if isinstance(provided, MigrationSettings):
        return provided
    return MigrationSettings()  # values come from the environment


def _refuse_cli_downgrade(app_env: AppEnv) -> None:
    command = getattr(config.cmd_opts, "cmd", None)
    if app_env.is_deployed and command and getattr(command[0], "__name__", "") == "downgrade":
        raise DowngradeNotAllowedError("alembic downgrade is not allowed in staging or production")


def _refuse_downgrade_step(
    app_env: AppEnv,
) -> Callable[[MigrationContext, MigrationInfo, Collection[Any], Mapping[str, Any]], None]:
    # Backstop for programmatic use: raising here rolls back the step's transaction.
    def check(
        ctx: MigrationContext, step: MigrationInfo, heads: Collection[Any], run_args: Mapping[str, Any]
    ) -> None:
        if app_env.is_deployed and not step.is_upgrade:
            raise DowngradeNotAllowedError("alembic downgrade is not allowed in staging or production")

    return check


def run_migrations_online() -> None:
    settings = _settings()
    if config.attributes.get("configure_logging", True):
        configure_logging(settings.log_level)
    _refuse_cli_downgrade(settings.app_env)

    engine = create_engine(
        sqlalchemy_url(settings.database_url_migrator),
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={"application_name": "hrms-migrator"},
    )
    try:
        with engine.connect() as connection:
            role: str = connection.execute(text("SELECT current_user")).scalar_one()
            if role != MIGRATOR_ROLE:
                raise RuntimeError(f"migrations must run as {MIGRATOR_ROLE}, not {role}")
            connection.execute(text(SET_LOCK_TIMEOUT))
            connection.commit()

            context.configure(
                connection=connection,
                target_metadata=metadata,
                transaction_per_migration=True,
                version_table_schema="public",
                on_version_apply=_refuse_downgrade_step(settings.app_env),
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline SQL generation is not supported; run migrations against the database")

run_migrations_online()
