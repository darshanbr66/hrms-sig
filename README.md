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

5. Run the web app (from `frontend/`; Node 20.19 or later, pnpm through corepack).

   ```sh
   corepack enable
   pnpm install
   pnpm dev
   ```

   Open `http://localhost:5173`. Vite forwards `/api` to the API on port 8000, so the app and
   the API share one origin, as in production. Set `APP_BASE_URL=http://localhost:5173`.

6. Create the first two super admins (once per installation). The command prints an invite link
   for each; there are no default credentials.

   ```sh
   uv run --env-file ../.env python -m app.cli bootstrap-super-admins --email <first> --email <second>
   ```

## Checks (from `backend/`)

| Check | Command |
|---|---|
| Format | `uv run ruff format --check .` |
| Lint | `uv run ruff check .` |
| Types | `uv run mypy .` |
| Module boundaries | `uv run lint-imports` |
| Tests (starts PostgreSQL and Redis containers) | `uv run pytest` |
| Dependency audit | `uv run pip-audit --strict` |
| Regenerate the API contract | `uv run python -m scripts.export_openapi` |

## Checks (from `frontend/`)

| Check | Command |
|---|---|
| Format | `pnpm format:check` |
| Lint (includes accessibility rules) | `pnpm lint` |
| Types | `pnpm typecheck` |
| Banned words in UI copy | `pnpm copy:check` |
| Tests | `pnpm test` |
| Build | `pnpm build` |
| Regenerate API types from `api/openapi.json` | `pnpm api:types` |

CI (`.github/workflows/ci.yml`) runs the same checks plus dependency audits, a gitleaks secret
scan and Semgrep.

## Rules for migrations

- Alembic is the only migration runner and always runs as `hrms_migrator`.
- Revisions are expand/contract (`docs/database-design.md` §11).
- Never run `alembic downgrade` or `procrastinate schema --apply` against staging or production.
