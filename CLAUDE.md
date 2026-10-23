# CLAUDE.md — Sigvitas HRMS

Internal Human Resource Management System for Sigvitas. Holds salary, personal, attendance and security data for every employee. Security and correctness come before speed of delivery.

## Current phase

**Phase 1 (M1, foundation) in progress.** Work follows the checkpoints in `docs/m1-implementation-plan.md` §10. Checkpoint A (repository, CI, local services, migrations, roles and grants, Procrastinate, rate limiter), Checkpoint B (audit streams and writer, org and people core tables, team resolution, import boundaries), Checkpoint C (identity, sign-in, sessions, MFA, step-up, authorization engine, role assignment, frontend sign-in shell), Checkpoint D (email outbox, admin invites, password reset, security emails, trusted devices, keyed attempted-email hash, settings registry), Checkpoint E (admin MFA reset and re-enrolment, grant requests with elevation, super admin assignment and break-glass, administrator-account disable rules) and Checkpoint F (audit sealer, signed checkpoints, write-once anchors, verification, retention with tombstones, tamper suite) are complete. Checkpoint G (the application UI: shell, dashboard, administration screens) was delivered on 2026-10-10 at the project owner's request together with the first slices of M2 (organization, employee records, personal tier, directory, self-service profile), M3 (attendance clock events only; no shifts or policies) and M4 (holiday calendars only), migrations `0014`–`0016`, followed on 2026-10-11 by M2 organization management (overview, archive guards, department descriptions, list filters and sorting, headcounts; migration `0017`) and by account linking, department heads and M4 leave management (configurable types and policies, ledger, requests, approvals, calendar; migration `0018`). On 2026-10-15 the project owner asked for the remaining HRMS work in one pass: notifications, shifts, attendance policies, day status, corrections and locks, leave accrual, year end and opening-balance import, documents with ClamAV scanning, exports, emergency contacts and the one-step exit (migrations `0019`–`0026`); see `docs/m1-implementation-plan.md` §12 for what is verified, what remains and why. On 2026-10-16: the organization add-dialog fix, viewport-safe dialogs with Playwright layout tests, declared working days in holiday calendars (migration `0027`), the onboarding and staff-provisioning journey tests, holiday calendar and record actions (`module-acceptance.md` §9), light/dark themes, the checkbox and scrolling fixes and the attendance, audit and department data views (§11, §12); on 2026-10-19 the final verification and the form-field border contrast fix (§13), then production deployment preparation (proposed only, `docs/deployment.md`; nothing deployed); on 2026-10-20 the project owner decided on MongoDB Atlas only with Render, Vercel and Resend (ADR-030), and the backend was moved from PostgreSQL, Redis and S3 to MongoDB (the PostgreSQL migrations `0001`–`0027` are replaced by `app.cli migrate`); module status and acceptance scripts are in `docs/module-acceptance.md`. On 2026-10-23 the project owner decided: administrator accounts may be linked to their holder's own employee record (self-service only, no derived manager role), several roles are assigned in one step, the super admin handover, HR correction of a forgotten clock-out, and permanent erasure of a person's data by two super admins (`docs/module-acceptance.md` §14, `docs/database-design.md` §9.1); migration version 2 gives existing accounts, employee records and document versions the new `erased_at` field, before the validators are applied (a new stored field always needs such a migration, `database-design.md` §11). Sigvitas HRMS is an HRMS, not a recruitment platform: no candidate, application or hiring-pipeline features. Do not start work beyond the current checkpoint without the user's approval. ADR statuses are in `docs/architecture-decisions.md`; open items are Sigvitas inputs listed in roadmap §6.

## Read before working

| Document | Use it for |
|---|---|
| `docs/product-requirements.md` | Scope, requirement IDs (AUTH-1, ATT-4…), MVP vs Phase 2 vs Future |
| `docs/architecture.md` | System shape, module layout, request lifecycle, mobile and AI readiness |
| `docs/security-architecture.md` | Auth, sessions, step-up, encryption, headers, audit |
| `docs/threat-model.md` | Threat scenarios T1–T26 and required controls |
| `docs/authorization-model.md` | Permission catalog, roles, scopes, SoD rules |
| `docs/roles-and-permissions.md` | Per-role pages, limits and workflows; the generated role matrix |
| `docs/database-design.md` | Data model, constraints, retention (written for PostgreSQL; ADR-030 and `app/platform/db.py` describe how MongoDB enforces it) |
| `docs/api-architecture.md` | REST conventions, errors, endpoint map, jobs |
| `docs/ui-ux-guidelines.md` | Design system direction, patterns, writing rules |
| `docs/engineering-principles.md` | Code rules, testing, definition of done |
| `docs/architecture-decisions.md` | ADRs and industry research |
| `docs/development-roadmap.md` | Milestones, M1 scope, Sigvitas inputs still needed (§6) |
| `docs/module-acceptance.md` | When a module counts as complete, each module's status, browser acceptance scripts, attendance location and payroll status |
| `docs/local-development.md` | Running the system locally, test sign-in, verification commands (the only local setup guide) |
| `docs/deployment.md` | Proposed production deployment (Vercel, Render, MongoDB Atlas, Resend): blockers, runbook, rollback. Nothing is deployed |

When a change alters behaviour, permissions, schema or API, update the relevant document in the same change.

## Stack (decided, see ADRs)

- Backend: Python 3.13, FastAPI, Pydantic v2, PyMongo (async API) with dataclass document models (`app/platform/db.py`), `python -m app.cli migrate` (the single migration runner: collections, `$jsonSchema` validators, indexes, numbered data migrations), a MongoDB lease scheduler for jobs (`app/platform/jobs.py`). Tooling: uv, Ruff, mypy strict, pytest + testcontainers.
- Frontend: React 19, TypeScript strict, Vite, Tailwind CSS v4, React Router (SPA), TanStack Query/Table, React Hook Form + Zod, React Aria Components, Lucide, Motion (sparingly). Tooling: pnpm, ESLint, Prettier, Vitest, Playwright, axe.
- Data (ADR-030): MongoDB Atlas only, database `hrms`, always a replica set (units of work are multi-document transactions): records, sessions, lockout, TTL-bound rate-limit counters, the email outbox, job leases, audit, and files in GridFS. Organizations are tenants; every tenant-owned document carries `organization_id`. ClamAV scans uploads; without it uploads are refused. No PostgreSQL, Redis, AWS or S3.
- Email: Resend's HTTPS API when deployed; SMTP to Mailpit locally only.
- Deployment (proposed, nothing deployed): SPA on Vercel forwarding `/api/*` to the API on Render (Docker web service, jobs inside the API with `RUN_JOBS_IN_API=true`), so one origin (`/` and `/api/v1`). No CORS.

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
13. Do not introduce a second database or data store (PostgreSQL, Redis, S3 or other), microservices, JWT access tokens, or a second component library without a new ADR approved by the user.
14. Business calculations (attendance durations, leave day counts, balances, pay) happen on the backend; the frontend formats.
15. MFA is mandatory for every account, employees included. Never add a password-only path, an MFA opt-out, or step-up by password. The last MFA factor can never be removed.
16. `super_admin` has no data access (salary, payroll, personal/sensitive data, documents, messages). Data roles come only through time-bound elevation approved by a different super admin; break-glass reaches only `system_admin` for 1 hour. Never grant data permissions to `super_admin` directly. The one owner-approved exception (2026-10-23) is destructive, not a read: `employee.erasure.*`, two super admins, showing the person's name and code only. A super admin linked to their own employee record gets self-service for that record only, never the derived manager role.
17. Every query of a tenant-owned collection goes through a tenant-scoped `Session`, which adds `organization_id`; `find_any_tenant` is only for resolving a sign-in, invitation or reset token to its organization. Rate-limit counters hold only HMAC-ed keys, times and an expiry, never personal data, and are never used as a cache.
18. Never hard-code Sigvitas HR policy values (grace, thresholds, weekly offs, overtime, leave types, accruals, leave year, identifier types, retention). They are effective-dated configuration; domain rules are pure functions over policy objects.
19. Audit records are never updated. Integrity comes from the sealer, the signed checkpoints and the daily audit witness (`app/platform/audit/witness.py`, ADR-030); the witness detects truncation, it does not prevent it, so never call the audit log tamper-proof. Audit months are removed only by the retention job, as its own database user, after a tombstone.
20. `python -m app.cli migrate` is the only migration runner. It never drops a collection or an index; data migrations are numbered, idempotent and never edited after they have run. Local and test processes refuse the production database name `hrms`, and every command that changes data needs `--confirm-database`. Never point a local or test command at the live Atlas database.

## Writing rules for UI copy and docs

Sentence case, plain words, no emoji in headings. Never use: seamless, cutting-edge, revolutionize, empower, skyrocket, next-generation, game-changing, delve, leverage, transformative, unlock, supercharge, effortless. Error messages say what happened and what to do next.

## Conventions

- Permission keys: `<domain>.<resource>.<action>[.<scope>]`, scopes `self | team | all`.
- Role keys: `employee` (derived), `manager` (derived), `hr`, `hr_admin`, `payroll_admin`, `system_admin`, `super_admin`, `auditor`.
- Attendance events: `CLOCK_IN`, `BREAK_START`, `BREAK_END`, `CLOCK_OUT` — signing in is not clocking in.
- IDs: UUIDv7, generated by the application, stored as BSON UUIDs. Timestamps: UTC BSON dates (millisecond precision). Money and day counts: `Decimal128` with two decimal places + currency; `Decimal` in Python; strings in JSON.
- Errors: RFC 9457 problem details with stable `type` values.
- Backend module layout: `router.py`, `schemas.py`, `service.py`, `repository.py`, `models.py`, `policies.py`, `events.py`, `public.py`. Cross-module imports only via `public.py`.
- Git: one branch, `main`. No feature branches, no pull requests, no force push. Commit directly to `main` after the checks in `docs/engineering-principles.md` §8 pass; deployment runs from `main`. Security-sensitive changes need a second-engineer review, or, only when the project owner authorizes it, a recorded adversarial self-review (§8). Never add a `Reviewed-by:` trailer for a review no human did.

## Commands

Whole local backend in one step (Windows, from the repository root): `.\run-backend.cmd` (local MongoDB replica set and Mailpit, target check, migrations, then the API with the jobs inside it; database `hrms_local`).

Local services (from the repository root): `docker compose -f infra/compose.yaml up -d --wait` (MongoDB `rs0` on `127.0.0.1:27017`, Mailpit)

Backend (from `backend/`):

| Task | Command |
|---|---|
| Install | `uv sync` |
| Check the target is a local, non-production database | `uv run --env-file ../.env python -m scripts.check_local_target --database hrms_local` |
| Migrate (as the migration user) | `uv run --env-file ../.env python -m app.cli migrate --confirm-database hrms_local` (`--status` only reports) |
| API | `uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop` |
| Worker (only with `RUN_JOBS_IN_API=false`) | `uv run --env-file ../.env python -m app.worker` |
| Format / lint / types | `uv run ruff format .` · `uv run ruff check .` · `uv run mypy .` |
| Module boundaries | `uv run lint-imports` |
| Tests (Docker required) | `uv run pytest` |
| Regenerate `api/openapi.json` | `uv run python -m scripts.export_openapi` |
| Create an organization (a tenant; once) | `uv run --env-file ../.env python -m app.cli create-organization --name "Local test company" --slug local-test --confirm-database hrms_local` |
| Create its first two super admins (once) | `uv run --env-file ../.env python -m app.cli bootstrap-super-admins --organization local-test --email A --email B --confirm-database hrms_local` |
| Print the Atlas custom roles (JSON) | `uv run python -m scripts.mongo_roles --database hrms` |
| Generate an audit signing key pair | `uv run python -m scripts.generate_audit_signing_key` |
| Regenerate the role matrix doc and web-app role fixture | `uv run python -m scripts.role_matrix` |
| Verify the audit chain and checkpoints (read-only) | `uv run --env-file ../.env python -m app.cli audit-verify [--witness FILE]` |
| Build the production image | `docker build -t hrms-backend .` |

Local email goes to Mailpit (`http://127.0.0.1:8025`); the jobs send it from the outbox. Documents and exports are in GridFS in the local database. ClamAV runs with the Compose `scan` profile (`127.0.0.1:3310`); without it uploads are refused (`DOCUMENT_UPLOADS_ENABLED=false`).

Frontend (from `frontend/`, pnpm through corepack):

| Task | Command |
|---|---|
| Install | `pnpm install` |
| Dev server (forwards `/api` to the API) | `pnpm dev` |
| Format / lint / types | `pnpm format:check` · `pnpm lint` · `pnpm typecheck` |
| UI copy (banned words) | `pnpm copy:check` |
| Tests / build | `pnpm test` · `pnpm build` |
| Browser layout tests (Playwright, fake API, own Vite on 5180) | `pnpm test:e2e` |
| Production build under the `vercel.json` headers and CSP | `pnpm test:csp` |
| Regenerate API types after the contract changes | `pnpm api:types` |

The selector event loop flag dates from the psycopg driver; PyMongo does not need it, and the documented commands keep it so local runs match `run-backend.ps1`.
