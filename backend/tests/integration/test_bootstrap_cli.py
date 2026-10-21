"""Operator commands (docs/authorization-model.md §4.3, security-architecture §3.1): an
organization and its first super admins, and the guards that keep data-changing commands on the
database they were meant for."""

import secrets

import pytest
from pydantic import ValidationError

from app import cli
from app.modules.access.public import BootstrapRefusedError
from app.platform.config import AppEnv, CliSettings
from tests.conftest import MongoServer, database_as, fresh_migrated_database


def settings_for(mongo: MongoServer, database: str) -> CliSettings:
    return CliSettings(
        app_env=AppEnv.TEST,
        mongodb_uri=mongo.uri("hrms_app"),
        mongodb_database=database,
        app_base_url="https://hrms.test",
    )


async def organization_in(mongo: MongoServer, database: str) -> str:
    slug = f"test-{secrets.token_hex(4)}"
    assert await cli.create_organization(settings_for(mongo, database), "Test company", slug) == 0
    return slug


async def test_bootstrap_creates_two_invited_super_admins_once(migrated_mongo: MongoServer) -> None:
    database = await fresh_migrated_database(migrated_mongo)
    settings = settings_for(migrated_mongo, database)
    slug = await organization_in(migrated_mongo, database)
    links = await cli.bootstrap_super_admins(settings, slug, ["first@dev.example", "second@dev.example"])
    assert set(links) == {"first@dev.example", "second@dev.example"}
    assert all(link.startswith("https://hrms.test/invite/") for link in links.values())

    async with database_as(migrated_mongo, "hrms_worker", database) as db:
        users = await db.collection("users").find({}).to_list()
        roles = await db.collection("user_roles").find({"role_key": "super_admin"}).to_list()
        assert sorted(user["status"] for user in users) == ["invited", "invited"]
        assert {role["user_id"] for role in roles} == {user["_id"] for user in users}
        assert [role["granted_by"] for role in roles] == [None, None]
        # No credentials exist yet: each person sets a password and MFA through the link.
        assert await db.collection("credentials").count_documents({}) == 0
        audit = (
            await db.collection("audit_log").find({"action": "access.super_admins.bootstrapped"}).to_list()
        )
        assert [item["actor_type"] for item in audit] == ["system"]

    with pytest.raises(BootstrapRefusedError, match="already has super admins"):
        await cli.bootstrap_super_admins(settings, slug, ["third@dev.example", "fourth@dev.example"])


@pytest.mark.parametrize(
    "emails",
    [
        ["only@dev.example"],
        ["same@dev.example", "SAME@dev.example"],
        ["a@dev.example", "b@dev.example", "c@dev.example"],
    ],
)
async def test_bootstrap_needs_exactly_two_different_people(
    migrated_mongo: MongoServer, emails: list[str]
) -> None:
    database = await fresh_migrated_database(migrated_mongo)
    slug = await organization_in(migrated_mongo, database)
    with pytest.raises(BootstrapRefusedError, match="exactly 2"):
        await cli.bootstrap_super_admins(settings_for(migrated_mongo, database), slug, emails)


@pytest.fixture
def local_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local shell's environment: nothing listens at this address, and nothing below connects."""
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1/?directConnection=true")
    monkeypatch.setenv("MONGODB_DATABASE", "hrms_local")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:5173")


@pytest.mark.usefixtures("local_environment")
@pytest.mark.parametrize(
    "arguments",
    [
        ["migrate"],
        ["migrate", "--confirm-database", "hrms_other"],
        ["create-organization", "--name", "Test", "--slug", "test-co", "--confirm-database", "hrms_other"],
        [
            "bootstrap-super-admins",
            "--organization",
            "test-co",
            "--email",
            "a@dev.example",
            "--email",
            "b@dev.example",
            "--confirm-database",
            "hrms",
        ],
    ],
)
def test_data_changing_commands_need_the_database_named_again(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == 1
    assert "does not match MONGODB_DATABASE (hrms_local). Nothing was changed." in capsys.readouterr().err


@pytest.mark.usefixtures("local_environment")
def test_local_commands_refuse_the_production_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGODB_DATABASE", "hrms")
    for arguments in (
        ["migrate", "--confirm-database", "hrms"],
        ["create-organization", "--name", "Test", "--slug", "test-co", "--confirm-database", "hrms"],
    ):
        with pytest.raises(ValidationError, match="production database"):
            cli.main(arguments)
