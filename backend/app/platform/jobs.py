"""Procrastinate job application (ADR-009).

The Procrastinate schema lives in the `procrastinate` schema. It is installed by Alembic
(revision 0002), never by `procrastinate schema --apply`, and the application roles find it
through their database-level search_path.
"""

import procrastinate
from pydantic import SecretStr


def create_job_app(database_url: SecretStr, *, application_name: str) -> procrastinate.App:
    connector = procrastinate.PsycopgConnector(
        conninfo=database_url.get_secret_value(),
        kwargs={"application_name": application_name},
    )
    return procrastinate.App(connector=connector)
