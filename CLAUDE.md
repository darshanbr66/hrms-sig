# CLAUDE.md — Sigvitas HRMS

Internal Human Resource Management System for Sigvitas. Holds salary, personal, attendance and security data for every employee. Security and correctness come before speed of delivery.

## Current phase

**Phase 1 (M1, foundation) in progress.** Work follows the checkpoints in `docs/m1-implementation-plan.md` §10. Checkpoint A (repository, CI, local services, migrations, roles and grants, Procrastinate, rate limiter) and Checkpoint B (audit streams and writer, org and people core tables, team resolution, import boundaries) are complete. Do not start work beyond the current checkpoint without the user's approval. ADR statuses are in `docs/architecture-decisions.md`; open items are Sigvitas inputs listed in roadmap §6.

## Read before working

| Document | Use it for |
|---|---|
| `docs/product-requirements.md` | Scope, requirement IDs (AUTH-1, ATT-4…), MVP vs Phase 2 vs Future |
| `docs/architecture.md` | System shape, module layout, request lifecycle, mobile and AI readiness |
| `docs/security-architecture.md` | Auth, sessions, step-up, encryption, headers, audit |
| `docs/threat-model.md` | Threat scenarios T1–T26 and required controls |
| `docs/authorization-model.md` | Permission catalog, roles, scopes, SoD rules |
| `docs/database-design.md` | Schemas, tables, constraints, retention |
| `docs/api-architecture.md` | REST conventions, errors, endpoint map, jobs |
| `docs/ui-ux-guidelines.md` | Design system direction, patterns, writing rules |
| `docs/engineering-principles.md` | Code rules, testing, definition of done |
| `docs/architecture-decisions.md` | ADRs and industry research |
| `docs/development-roadmap.md` | Milestones, M1 scope, Sigvitas inputs still needed (§6) |

When a change alters behaviour, permissions, schema or API, update the relevant document in the same change.

## Stack (decided, see ADRs)

- Backend: Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2.0 async with psycopg 3, Alembic (the single migration runner, also applying the vendored Procrastinate SQL), Procrastinate (PostgreSQL job queue). Tooling: uv, Ruff, mypy strict, pytest + testcontainers.
- Frontend: React 19, TypeScript strict, Vite, Tailwind CSS v4, React Router (SPA), TanStack Query/Table, React Hook Form + Zod, React Aria Components, Lucide, Motion (sparingly). Tooling: pnpm, ESLint, Prettier, Vitest, Playwright, axe.
- Data: PostgreSQL 18 (only system of record: data, jobs, sessions, lockout, audit), Redis (ephemeral rate-limit counters only; `docs/architecture.md` §3.1), private S3-compatible storage plus a write-once bucket for audit checkpoint anchors, ClamAV.
- Deployment: SPA and API on the same origin (`/` and `/api/v1`). No CORS.

## Rules that must never be broken

1. Every authorization decision happens in the backend service layer via `platform/authz`. Frontend permission checks are cosmetic.
2. Every route declares a permission or is on the explicit public allow-list. Every list query takes a scope filter.
3. Self-service endpoints live under `/me` and never take an employee ID.
4. Separate Pydantic models per audience; `extra="forbid"`. Personal, sensitive and restricted data are separate sub-resources.
5. Never update or delete attendance events, leave ledger entries, job history, compensation history, status history or audit rows. Add new records.
6. Audit records are written in the same transaction as the change. Sensitive values are referenced, not copied into audit.
7. No secrets, credentials or environment URLs in code. Configuration via typed settings from environment variables.
8. No personal data, tokens or request bodies in logs.
9. Documents only through authorized, audited, 60-second signed URLs. Never public URLs.
10. No invented data in the product: no fake employees, departments, statistics, policies or testimonials. Dev seed data is obviously fake and refuses to run in production.
11. No placeholder routes, dead buttons, stubbed endpoints, `TODO`s in completed features, `console.log`/`print` debugging, unused files or duplicate components.
12. Do not add dependencies without a stated reason (`docs/engineering-principles.md` §4). One library per concern.
13. Do not introduce MongoDB, microservices, JWT access tokens, or a second component library without a new ADR approved by the user.
14. Business calculations (attendance durations, leave day counts, balances, pay) happen on the backend; the frontend formats.
15. MFA is mandatory for every account, employees included. Never add a password-only path, an MFA opt-out, or step-up by password. The last MFA factor can never be removed.
16. `super_admin` has no data access (salary, payroll, personal/sensitive data, documents, messages). Data roles come only through time-bound elevation approved by a different super admin; break-glass reaches only `system_admin` for 1 hour. Never grant data permissions to `super_admin` directly.
17. Redis stores only TTL-bound rate-limit counters with HMAC-ed keys. Never put data, sessions, permissions, lockout state, audit, jobs or caches of personal data in Redis.
18. Never hard-code Sigvitas HR policy values (grace, thresholds, weekly offs, overtime, leave types, accruals, leave year, identifier types, retention). They are effective-dated configuration; domain rules are pure functions over policy objects.
19. Audit rows are never updated. Integrity comes from the sealer, the signed checkpoints and the write-once anchors (`docs/security-architecture.md` §8.1). Audit partitions are removed only by the retention job after a tombstone.
20. Alembic is the only migration runner. Revisions are expand/contract. Never run `procrastinate schema --apply` or `alembic downgrade` against staging or production.

## Writing rules for UI copy and docs

Sentence case, plain words, no emoji in headings. Never use: seamless, cutting-edge, revolutionize, empower, skyrocket, next-generation, game-changing, delve, leverage, transformative, unlock, supercharge, effortless. Error messages say what happened and what to do next.

## Conventions

- Permission keys: `<domain>.<resource>.<action>[.<scope>]`, scopes `self | team | all`.
- Role keys: `employee` (derived), `manager` (derived), `hr`, `hr_admin`, `payroll_admin`, `system_admin`, `super_admin`, `auditor`.
- Attendance events: `CLOCK_IN`, `BREAK_START`, `BREAK_END`, `CLOCK_OUT` — signing in is not clocking in.
- IDs: UUIDv7. Timestamps: `timestamptz` UTC. Money: `numeric(14,2)` + currency; `Decimal` in Python; strings in JSON.
- Errors: RFC 9457 problem details with stable `type` values.
- Backend module layout: `router.py`, `schemas.py`, `service.py`, `repository.py`, `models.py`, `policies.py`, `events.py`, `public.py`. Cross-module imports only via `public.py`.
- Git: one branch, `main`. No feature branches, no pull requests, no force push. Commit directly to `main` after the checks in `docs/engineering-principles.md` §8 pass; deployment runs from `main`. Security-sensitive changes need a second-engineer review, or, only when the project owner authorizes it, a recorded adversarial self-review (§8). Never add a `Reviewed-by:` trailer for a review no human did.

## Commands

Local services (from the repository root): `docker compose --env-file .env -f infra/compose.yaml up -d --wait`

Backend (from `backend/`):

| Task | Command |
|---|---|
| Install | `uv sync` |
| Migrate (as `hrms_migrator`) | `uv run --env-file ../.env alembic upgrade head` |
| API | `uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop` |
| Worker | `uv run --env-file ../.env python -m app.worker` |
| Format / lint / types | `uv run ruff format .` · `uv run ruff check .` · `uv run mypy .` |
| Module boundaries | `uv run lint-imports` |
| Tests (Docker required) | `uv run pytest` |
| Regenerate `api/openapi.json` | `uv run python -m scripts.export_openapi` |

The selector event loop is required because psycopg's async driver does not support the Windows Proactor loop.
