# Sigvitas HRMS

Internal human resource management system for Sigvitas. Start with `CLAUDE.md` and the documents in `docs/`.

Status: Phase 1 (M1, foundation) in progress. See `docs/m1-implementation-plan.md`.

## Prerequisites

- Docker with Docker Compose
- [uv](https://docs.astral.sh/uv/) (installs Python 3.13 for the backend automatically)

## Local setup

1. Create your local configuration and fill in every value. Generate secrets as described in the file.

   ```sh
   cp .env.example .env
   ```

   The `DATABASE_URL_*` and `REDIS_URL` values use the passwords you chose above and the local ports
   5433 (PostgreSQL) and 6380 (Redis).

2. Start PostgreSQL 18 and Redis. The first start creates the database roles from
   `infra/db/bootstrap-roles.sql`.

   ```sh
   docker compose --env-file .env -f infra/compose.yaml up -d --wait
   ```

3. Install backend dependencies and apply migrations (runs as `hrms_migrator`).

   ```sh
   cd backend
   uv sync
   uv run --env-file ../.env alembic upgrade head
   ```

4. Run the API and the worker (from `backend/`).

   ```sh
   uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
   uv run --env-file ../.env python -m app.worker
   ```

   `GET http://127.0.0.1:8000/api/health/ready` returns `{"status": "ok"}` when the API can reach the
   database.

## Checks (from `backend/`)

| Check | Command |
|---|---|
| Format | `uv run ruff format --check .` |
| Lint | `uv run ruff check .` |
| Types | `uv run mypy .` |
| Tests (starts PostgreSQL and Redis containers) | `uv run pytest` |
| Dependency audit | `uv run pip-audit --strict` |
| Regenerate the API contract | `uv run python -m scripts.export_openapi` |

CI (`.github/workflows/ci.yml`) runs the same checks plus a gitleaks secret scan and Semgrep.

## Rules for migrations

- Alembic is the only migration runner and always runs as `hrms_migrator`.
- Revisions are expand/contract (`docs/database-design.md` §11).
- Never run `alembic downgrade` or `procrastinate schema --apply` against staging or production.
