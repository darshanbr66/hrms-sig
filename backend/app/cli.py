"""Operator commands. Run from backend/ with the environment of the target installation:

    uv run --env-file ../.env python -m app.cli migrate --confirm-database NAME   (or --status)
    uv run --env-file ../.env python -m app.cli create-organization --name "Company" --slug company
        --confirm-database NAME
    uv run --env-file ../.env python -m app.cli bootstrap-super-admins --organization company --email A
        --email B --confirm-database NAME
    uv run --env-file ../.env python -m app.cli audit-verify [--scope recent|full] [--witness FILE]
    uv run --env-file ../.env python -m app.cli audit-export-checkpoints --out DIRECTORY

Commands that change data refuse to run unless `--confirm-database` repeats MONGODB_DATABASE, and
in local and test (APP_ENV) the settings refuse the production database's name (`hrms`).

- `migrate` (as the migration database user) creates the declared collections, validators and
  indexes and applies pending versioned migrations (docs/database-design.md §11). It never
  drops anything; it stops, changing nothing, if existing data does not match a validator or an
  index exists with another definition. `--status` only reports.
- `create-organization` creates an organization (a tenant). There is no API for it.
- `bootstrap-super-admins` creates an organization's first two super admins as invited accounts
  and prints their invite links (docs/security-architecture.md §3.1, docs/authorization-model.md
  §4.3). There are no default credentials: each person sets a password and enrols an
  authenticator through the link. The links are secrets; deliver each one to its owner over a
  trusted channel. It refuses to run when the organization already has super admins.
- `audit-verify` checks both audit streams' chains, checkpoints and signatures
  (docs/security-architecture.md §8.1) and prints every problem it finds; it exits with code 1
  if there is any. It only reads (as the worker's database user, with the public verification
  keys) and records nothing; the scheduled jobs raise the alerts. `--witness FILE` also compares
  the witness lines saved from the daily witness emails with the database
  (app/platform/audit/witness.py).
- `audit-export-checkpoints` writes every signed checkpoint, as canonical JSON, into a directory
  outside the database (one file per checkpoint, existing files left alone). Keep the copies
  where database administrators cannot change them; comparing them with the database is how a
  rewrite of the audit trail by someone with database access is detected.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr

from app.metadata import MODELS
from app.modules.access import public as access
from app.platform.audit import chain, witness
from app.platform.audit.canonical import Stream
from app.platform.audit.records import AuditActor, AuditEvent
from app.platform.audit.verify import Report, Scope, verify_stream
from app.platform.audit.writer import AuditWriter
from app.platform.clock import SystemClock
from app.platform.config import AuditVerifySettings, CliSettings, MigrationSettings
from app.platform.db import CASE_INSENSITIVE, Database, UniqueViolationError, create_client, tenant_scope
from app.platform.files import FILE_INDEXES
from app.platform.logging import configure_logging
from app.platform.schema import SchemaError, migrate, status
from app.platform.tenancy import Organization
from migrations import MIGRATIONS

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def _database(uri: SecretStr, name: str, application: str) -> Database:
    return Database(create_client(uri, application_name=application), name)


async def run_migrations(settings: MigrationSettings, *, only_status: bool) -> int:
    database = _database(settings.mongodb_uri, settings.mongodb_database, "hrms-migrate")
    try:
        if only_status:
            done, pending = await status(database, MIGRATIONS)
            sys.stdout.write(f"applied: {done or 'none'}; pending: {pending or 'none'}\n")
            return 0
        report = await migrate(database, MODELS, MIGRATIONS, FILE_INDEXES)
    except SchemaError as exc:
        sys.stderr.write(f"Stopped, nothing changed by this step: {exc}.\n")
        return 1
    finally:
        await database.dispose()
    sys.stdout.write(
        f"collections created: {len(report.created_collections)}; indexes created: "
        f"{len(report.created_indexes)}; versions applied: {report.applied_versions or 'none'}\n"
    )
    for name in report.undeclared_indexes:
        sys.stdout.write(f"  index not declared (left in place): {name}\n")
    for name in report.undeclared_collections:
        sys.stdout.write(f"  collection not declared (left in place): {name}\n")
    return 0


async def create_organization(settings: CliSettings, name: str, slug: str) -> int:
    if not _SLUG.fullmatch(slug):
        sys.stderr.write("Refused: the slug is 2 to 63 lower-case letters, digits and hyphens.\n")
        return 1
    database = _database(settings.mongodb_uri, settings.mongodb_database, "hrms-cli")
    try:
        organization = Organization(name=name.strip(), slug=slug)
        async with database.unit_of_work() as session:
            session.add(organization)
        with tenant_scope(organization.id):
            async with database.unit_of_work() as session:
                await AuditWriter(SystemClock()).record(
                    session, AuditEvent(action="tenancy.organization.created", actor=AuditActor.system())
                )
    except UniqueViolationError:
        sys.stderr.write(f"Refused: an organization with the slug {slug} already exists.\n")
        return 1
    finally:
        await database.dispose()
    sys.stdout.write(f"Created organization {slug} ({organization.id}).\n")
    return 0


async def bootstrap_super_admins(settings: CliSettings, slug: str, emails: Sequence[str]) -> dict[str, str]:
    """Returns email -> invite URL."""
    clock = SystemClock()
    writer = AuditWriter(clock)
    database = _database(settings.mongodb_uri, settings.mongodb_database, "hrms-cli")
    try:
        found = await database.collection(Organization.__collection__).find_one(
            {"slug": slug}, collation=CASE_INSENSITIVE
        )
        if found is None:
            raise access.BootstrapRefusedError(f"there is no organization {slug}")
        with tenant_scope(found["_id"]):
            async with database.unit_of_work() as session:
                invites = await access.bootstrap_super_admins(session, list(emails), clock.now())
                await writer.record(
                    session,
                    AuditEvent(action="access.super_admins.bootstrapped", actor=AuditActor.system()),
                )
    finally:
        await database.dispose()
    return {email: f"{settings.app_base_url}/invite/{token}" for email, token in invites.items()}


async def audit_verify(settings: AuditVerifySettings, scope: Scope) -> list[Report]:
    database = _database(settings.mongodb_uri, settings.mongodb_database, "hrms-cli")
    try:
        verifier, now = settings.signature_verifier(), SystemClock().now()
        return [
            await verify_stream(database, verifier, stream=stream, scope=scope, now=now) for stream in Stream
        ]
    finally:
        await database.dispose()


async def verify_witness_lines(settings: AuditVerifySettings, lines: Sequence[witness.WitnessLine]) -> int:
    database = _database(settings.mongodb_uri, settings.mongodb_database, "hrms-cli")
    try:
        findings = await witness.verify_witness(database, settings.signature_verifier(), lines)
    finally:
        await database.dispose()
    outcome = "OK" if not findings else "FAILED"
    sys.stdout.write(f"witness: {outcome} ({len(lines)} witnessed checkpoints)\n")
    for finding in findings:
        sys.stdout.write(f"  {finding.check} [{finding.stream}]: {finding.detail}\n")
    return 0 if not findings else 1


def _confirmed(database: str, confirmation: str) -> bool:
    """Commands that change data name the database they change, so a stale MONGODB_DATABASE in
    the shell cannot send them to another one."""
    if confirmation == database:
        return True
    sys.stderr.write(
        f"Refused: --confirm-database {confirmation} does not match MONGODB_DATABASE ({database}). "
        "Nothing was changed.\n"
    )
    return False


async def export_checkpoints(settings: AuditVerifySettings, directory: Path) -> int:
    database = _database(settings.mongodb_uri, settings.mongodb_database, "hrms-cli")
    written = 0
    try:
        for stream in Stream:
            async with database.unit_of_work() as session:
                stored = await chain.checkpoints(session, stream)
            target = directory / stream.value
            target.mkdir(parents=True, exist_ok=True)
            for item in stored:
                path = target / f"{item.fields.checkpoint_no:012d}.json"
                if path.exists():
                    if path.read_bytes() != item.body:
                        sys.stderr.write(f"MISMATCH: {path} differs from the database's checkpoint.\n")
                        return 1
                    continue
                path.write_bytes(item.body)
                written += 1
    finally:
        await database.dispose()
    sys.stdout.write(f"{written} new checkpoint file(s) written to {directory}.\n")
    return 0


def _print_reports(reports: Sequence[Report]) -> None:
    for report in reports:
        sys.stdout.write(
            f"{report.stream.value}: {'OK' if report.ok else 'FAILED'} ({report.scope.value}; "
            f"{report.checkpoints_checked} checkpoints, "
            f"{len(report.partitions_checked)} months, {report.rows_checked} records)\n"
        )
        for finding in report.findings:
            where = f" [{finding.partition_key}]" if finding.partition_key else ""
            sys.stdout.write(f"  {finding.check}{where}: {finding.detail}\n")


def _migrate_command(arguments: argparse.Namespace) -> int:
    settings = MigrationSettings()  # values come from the environment
    configure_logging(settings.log_level)
    if not arguments.status and not _confirmed(settings.mongodb_database, arguments.confirm_database or ""):
        return 1
    return asyncio.run(run_migrations(settings, only_status=arguments.status))


def _audit_command(arguments: argparse.Namespace) -> int:
    settings = AuditVerifySettings()  # values come from the environment
    configure_logging(settings.log_level)
    if arguments.command == "audit-export-checkpoints":
        return asyncio.run(export_checkpoints(settings, arguments.out))
    witnessed: list[witness.WitnessLine] = []
    if arguments.witness is not None:
        witnessed = witness.parse_lines(arguments.witness.read_text(encoding="utf-8").splitlines())
        if not witnessed:
            sys.stderr.write(f"No witness lines found in {arguments.witness}.\n")
            return 1
    reports = asyncio.run(audit_verify(settings, Scope(arguments.scope)))
    _print_reports(reports)
    ok = all(report.ok for report in reports)
    if witnessed:
        ok = asyncio.run(verify_witness_lines(settings, witnessed)) == 0 and ok
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    migration = commands.add_parser("migrate", help="create collections and indexes, apply migrations")
    migration.add_argument("--status", action="store_true", help="only report applied and pending versions")
    organization = commands.add_parser("create-organization", help="create an organization (a tenant)")
    organization.add_argument("--name", required=True)
    organization.add_argument("--slug", required=True)
    bootstrap = commands.add_parser(
        "bootstrap-super-admins", help="create an organization's first two super admins"
    )
    bootstrap.add_argument("--organization", required=True, help="the organization's slug")
    bootstrap.add_argument(
        "--email", action="append", required=True, help="repeat for each of the two people"
    )
    verify = commands.add_parser("audit-verify", help="verify the audit chain and checkpoints (read-only)")
    verify.add_argument("--scope", choices=[scope.value for scope in Scope], default=Scope.FULL.value)
    verify.add_argument(
        "--witness", type=Path, help="also compare the witness lines saved in this file with the database"
    )
    export = commands.add_parser("audit-export-checkpoints", help="copy signed checkpoints to a directory")
    export.add_argument("--out", required=True, type=Path)
    for changing in (migration, organization, bootstrap):
        changing.add_argument(
            "--confirm-database",
            required=changing is not migration,
            help="the MONGODB_DATABASE this command changes, typed again",
        )
    arguments = parser.parse_args(argv)

    if arguments.command == "migrate":
        return _migrate_command(arguments)
    if arguments.command in ("audit-verify", "audit-export-checkpoints"):
        return _audit_command(arguments)

    settings = CliSettings()  # values come from the environment
    configure_logging(settings.log_level)
    if not _confirmed(settings.mongodb_database, arguments.confirm_database):
        return 1
    if arguments.command == "create-organization":
        return asyncio.run(create_organization(settings, arguments.name, arguments.slug))
    try:
        links = asyncio.run(bootstrap_super_admins(settings, arguments.organization, arguments.email))
    except access.BootstrapRefusedError as exc:
        sys.stderr.write(f"Refused: {exc}.\n")
        return 1
    sys.stdout.write("Invite links (valid for 72 hours, single use). Deliver each to its owner only:\n")
    for email, link in links.items():
        sys.stdout.write(f"  {email}: {link}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
