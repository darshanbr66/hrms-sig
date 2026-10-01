"""Migration history rules (docs/database-design.md §11, docs/engineering-principles.md §7)."""

import hashlib
from importlib import resources
from pathlib import Path

import procrastinate
import pytest
from alembic import command
from alembic.script import ScriptDirectory

from app.platform.config import AppEnv, MigrationSettings
from tests.conftest import BACKEND_DIR, PostgresServer, alembic_config, fresh_database, migration_settings

VENDOR_ROOT = BACKEND_DIR / "migrations" / "vendor" / "procrastinate"


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config(MigrationSettings.model_construct()))


def test_history_is_linear_with_a_single_head() -> None:
    scripts = script_directory()
    assert len(scripts.get_heads()) == 1
    for revision in scripts.walk_revisions():
        assert not revision.is_merge_point
        assert not revision.is_branch_point


def test_vendored_procrastinate_sql_matches_checksums_and_installed_release() -> None:
    [version_dir] = [path for path in VENDOR_ROOT.iterdir() if path.is_dir()]
    assert version_dir.name == procrastinate.__version__
    recorded: dict[str, str] = {}
    for line in (version_dir / "CHECKSUMS").read_text().splitlines():
        digest, name = line.split()
        recorded[name] = digest
    vendored_files = {path.name for path in version_dir.iterdir() if path.name != "CHECKSUMS"}
    assert vendored_files == set(recorded)
    package_sql = resources.files("procrastinate") / "sql"
    for name, digest in recorded.items():
        vendored = (version_dir / name).read_bytes()
        assert hashlib.sha256(vendored).hexdigest() == digest
        assert (package_sql / name).read_bytes() == vendored


def schemas(postgres: PostgresServer, database: str) -> set[str]:
    with postgres.connect("postgres", database) as connection:
        rows = connection.execute(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'"
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_downgrade_upgrade_cycle(postgres: PostgresServer) -> None:
    database = fresh_database(postgres)
    config = alembic_config(migration_settings(postgres.url("hrms_migrator", database)))

    command.upgrade(config, "head")
    upgraded = schemas(postgres, database)
    assert {"identity", "access", "org", "people", "notify", "app", "audit", "procrastinate"} <= upgraded

    command.downgrade(config, "base")
    assert schemas(postgres, database) == {"public"}
    with postgres.connect("postgres", database) as connection:
        leftover_settings = connection.execute(
            "SELECT count(*) FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase "
            "WHERE d.datname = current_database()"
        ).fetchone()
        assert leftover_settings == (0,)
        leftover_default_acls = connection.execute("SELECT count(*) FROM pg_default_acl").fetchone()
        assert leftover_default_acls == (0,)

    command.upgrade(config, "head")
    assert schemas(postgres, database) == upgraded


def test_migrations_refuse_to_run_as_another_role(postgres: PostgresServer) -> None:
    database = fresh_database(postgres)
    config = alembic_config(migration_settings(postgres.url("postgres", database)))
    with pytest.raises(RuntimeError, match="must run as hrms_migrator"):
        command.upgrade(config, "head")


def test_downgrade_is_refused_in_deployed_environments(postgres: PostgresServer) -> None:
    database = fresh_database(postgres)
    url = postgres.url("hrms_migrator", database)
    command.upgrade(alembic_config(migration_settings(url)), "head")

    # model_construct skips the TLS requirement so the guard itself is what is tested.
    production = MigrationSettings.model_construct(
        app_env=AppEnv.PRODUCTION, log_level="INFO", database_url_migrator=url
    )
    with pytest.raises(RuntimeError, match="downgrade is not allowed"):
        command.downgrade(alembic_config(production), "-1")
    assert "procrastinate" in schemas(postgres, database)


def test_vendored_files_are_kept_byte_for_byte_by_git() -> None:
    attributes = (BACKEND_DIR.parent / ".gitattributes").read_text()
    assert "backend/migrations/vendor/** -text" in attributes
    assert Path(VENDOR_ROOT).is_dir()
