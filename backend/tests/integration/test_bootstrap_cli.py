"""The installation bootstrap command (docs/authorization-model.md §4.3, security-architecture §3.1)."""

import pytest
from sqlalchemy import text

from app import cli
from app.modules.access.public import BootstrapRefusedError
from app.platform.config import AppEnv, CliSettings
from tests.conftest import PostgresServer, database_as, fresh_migrated_database


def settings_for(postgres: PostgresServer, database: str) -> CliSettings:
    return CliSettings(
        app_env=AppEnv.TEST,
        database_url_app=postgres.url("hrms_app", database),
        app_base_url="https://hrms.test",
    )


async def test_bootstrap_creates_two_invited_super_admins_once(postgres: PostgresServer) -> None:
    database = fresh_migrated_database(postgres)
    settings = settings_for(postgres, database)
    links = await cli.bootstrap_super_admins(settings, ["first@dev.example", "second@dev.example"])
    assert set(links) == {"first@dev.example", "second@dev.example"}
    assert all(link.startswith("https://hrms.test/invite/") for link in links.values())

    async with database_as(postgres, "hrms_app", database) as db, db.unit_of_work() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT u.status, ur.granted_by FROM identity.users u "
                    "JOIN access.user_roles ur ON ur.user_id = u.id "
                    "JOIN access.roles r ON r.id = ur.role_id WHERE r.key = 'super_admin'"
                )
            )
        ).all()
        assert sorted(rows) == [("invited", None), ("invited", None)]
        # No credentials exist yet: each person sets a password and MFA through the link.
        assert (await session.execute(text("SELECT count(*) FROM identity.credentials"))).scalar_one() == 0
        audit = await session.execute(
            text("SELECT actor_type FROM audit.audit_log WHERE action = 'access.super_admins.bootstrapped'")
        )
        assert audit.all() == [("system",)]

    with pytest.raises(BootstrapRefusedError, match="already has super admins"):
        await cli.bootstrap_super_admins(settings, ["third@dev.example", "fourth@dev.example"])


@pytest.mark.parametrize(
    "emails",
    [
        ["only@dev.example"],
        ["same@dev.example", "SAME@dev.example"],
        ["a@dev.example", "b@dev.example", "c@dev.example"],
    ],
)
async def test_bootstrap_needs_exactly_two_different_people(
    postgres: PostgresServer, emails: list[str]
) -> None:
    database = fresh_migrated_database(postgres)
    with pytest.raises(BootstrapRefusedError, match="exactly 2"):
        await cli.bootstrap_super_admins(settings_for(postgres, database), emails)
