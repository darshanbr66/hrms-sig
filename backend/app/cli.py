"""Operator commands. Run from backend/ with the environment of the target installation:

    uv run --env-file ../.env python -m app.cli bootstrap-super-admins --email A --email B

`bootstrap-super-admins` creates the first two super admins as invited accounts and prints
their invite links (docs/security-architecture.md §3.1, docs/authorization-model.md §4.3).
There are no default credentials: each person sets a password and enrols an authenticator
through the link. The links are secrets; deliver each one to its owner over a trusted
channel. The command refuses to run when the installation already has super admins.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

# Registers every table, so foreign keys into other modules (such as org.locations) resolve.
import app.metadata  # noqa: F401
from app.modules.access import public as access
from app.platform.audit.records import AuditActor, AuditEvent
from app.platform.audit.writer import AuditWriter
from app.platform.clock import SystemClock
from app.platform.config import CliSettings
from app.platform.db import Database, create_engine
from app.platform.logging import configure_logging


async def bootstrap_super_admins(settings: CliSettings, emails: Sequence[str]) -> dict[str, str]:
    """Returns email -> invite URL."""
    clock = SystemClock()
    writer = AuditWriter(clock)
    database = Database(create_engine(settings.database_url_app, application_name="hrms-cli"))
    try:
        async with database.unit_of_work() as session:
            invites = await access.bootstrap_super_admins(session, list(emails), clock.now())
            await writer.record(
                session,
                AuditEvent(action="access.super_admins.bootstrapped", actor=AuditActor.system()),
            )
    finally:
        await database.dispose()
    return {email: f"{settings.app_base_url}/invite/{token}" for email, token in invites.items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-super-admins", help="create the first two super admins")
    bootstrap.add_argument(
        "--email", action="append", required=True, help="repeat for each of the two people"
    )
    arguments = parser.parse_args(argv)

    settings = CliSettings()  # values come from the environment
    configure_logging(settings.log_level)
    # psycopg's async driver needs a selector event loop, which is not the Windows default.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        links = asyncio.run(bootstrap_super_admins(settings, arguments.email), loop_factory=loop_factory)
    except access.BootstrapRefusedError as exc:
        sys.stderr.write(f"Refused: {exc}.\n")
        return 1
    sys.stdout.write("Invite links (valid for 72 hours, single use). Deliver each to its owner only:\n")
    for email, link in links.items():
        sys.stdout.write(f"  {email}: {link}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
