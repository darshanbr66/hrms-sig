# Sigvitas HRMS — Engineering Principles

Status: Draft v0.1. These rules apply to every change, human- or AI-written.

## 1. Non-negotiables

1. Authorization is enforced in the backend service layer for every operation. The frontend is never a control.
2. No secrets, credentials, API keys or environment-specific URLs in source code. Configuration comes from environment variables validated at startup.
3. No fake implementations: no stubbed endpoints returning canned data, no dead buttons, no placeholder routes, no "coming soon" pages. A feature ships complete or it does not ship.
4. No `TODO`/`FIXME` in merged code for completed features. Deferred work is a tracked issue, referenced by ID in a comment only when the code is intentionally partial and guarded.
5. No `console.log`/`print` debugging left in code. Use the logger.
6. Every change to authentication, authorization, payroll, documents or audit requires review by a second engineer (CODEOWNERS).
7. Data history is preserved: no destructive updates of attendance events, ledger entries, job history, compensation history or audit records.
8. No invented business data in the product.
9. No Sigvitas HR policy value in code, migrations or seed data (ADR-026). Grace periods, thresholds, weekly offs, leave types, accrual amounts, leave year start, identifier types and retention periods are configuration. Tests that need values use fixtures named `TEST – …`.
10. MFA is mandatory for every account. No code path, flag or setting creates a session from a password alone (ADR-008).
11. Redis holds only ephemeral rate-limit counters (ADR-010). Any other use needs a new ADR.

## 2. Repository layout

```
hrms/
  CLAUDE.md
  docs/                 # this documentation set
  backend/              # FastAPI app, Alembic, tests (Python, uv)
  frontend/             # React SPA (TypeScript, pnpm)
  api/openapi.json      # committed contract, generated
  infra/                # Dockerfiles, compose for local, deployment config
  .github/workflows/    # CI (or equivalent)
```

One repository, two applications, one contract.

## 3. Tooling

| Concern | Backend | Frontend |
|---|---|---|
| Package / env manager | `uv` with `uv.lock` | `pnpm` with `pnpm-lock.yaml` |
| Formatting + lint | Ruff (format + lint incl. `S` security, `B`, `UP`, `SIM`, `I`) | ESLint (typescript-eslint strict, react-hooks, jsx-a11y) + Prettier |
| Types | mypy `--strict` with the Pydantic plugin | `tsc --noEmit` with `strict`, `noUncheckedIndexedAccess` |
| Tests | pytest, pytest-asyncio, testcontainers (real PostgreSQL) | Vitest + Testing Library; Playwright for end-to-end |
| Architecture checks | import-linter (module boundaries) | ESLint `no-restricted-imports` for feature boundaries |
| Security | Semgrep, pip-audit, gitleaks | pnpm audit, gitleaks |
| Accessibility | — | axe-core in Vitest and Playwright |

Local services (PostgreSQL, Redis, object storage emulator, ClamAV, mail catcher) via Docker Compose in `infra/`.

## 4. Dependencies

- Add a dependency only when it removes meaningful complexity or risk that we would otherwise own. Write the reason in the PR.
- Prefer the standard library and platform features (PostgreSQL features over new services; CSS over animation libraries; `Intl` over date formatting libraries where sufficient).
- Before adding: check maintenance activity, licence (permissive only), transitive dependency count, and known vulnerabilities.
- One library per concern. No second component library, no second date library, no second HTTP client.
- Lockfiles committed. Renovate/Dependabot weekly; security updates immediately.

## 5. Backend rules

- Module boundaries per `architecture.md` §4; enforced by import-linter in CI.
- Routers are thin. Business rules in services; queries in repositories; resource rules in `policies.py`.
- Every service method that touches employee data takes the actor context as its first argument.
- Every repository list function requires a scope filter argument.
- Pydantic models: `strict=True`, `extra="forbid"`, explicit max lengths, separate request and response models, separate models per audience.
- One unit of work per request; commit at the end of the service call. No commits inside repositories.
- Time: use an injected clock (`platform/clock.py`) so time-dependent logic (attendance, accrual, expiry) is testable. Never call `datetime.now()` directly in domain code.
- Money: `Decimal` only.
- Errors: raise domain exceptions; a single handler maps them to problem details.
- Logging: structured, via the platform logger, IDs not personal data.
- Migrations: follow `database-design.md` §11. One linear Alembic history applies both our schemas and the vendored Procrastinate SQL. Every revision is expand/contract-compatible with the previously deployed code and sets `lock_timeout`. Large backfills run as batched jobs. Staging and production are forward-only; `downgrade()` exists for local development and CI only.
- Domain rules for attendance, leave and (later) payroll are pure functions that take effective-dated policy objects as parameters. They read no settings, environment variables or constants for policy values.

## 6. Frontend rules

- Feature folders own their routes, components and query hooks. Shared UI lives in `design-system/` only.
- No business rules in components: calculations such as leave day counts, attendance durations and balances come from the API. The client formats; it does not decide.
- Server state via TanStack Query; forms via React Hook Form + Zod; no global state library unless justified in an ADR.
- API types generated from OpenAPI; never hand-written copies of API models.
- No hard-coded URLs; relative `/api` paths only.
- No `dangerouslySetInnerHTML` outside the single sanitized rich-text component.
- No inline styles; Tailwind classes using design tokens.
- Every interactive element keyboard-accessible with a visible focus state.
- Every list/page has loading, empty and error states.

## 7. Testing strategy

| Layer | What | Target |
|---|---|---|
| Domain unit tests | Attendance derivation, leave day counting, accrual, carry-forward/lapse, SoD rules, permission evaluation, validation | Exhaustive for rules; edge cases (midnight shifts, DST-free zones and zones with DST, half days, holidays on weekly offs, leap years, leave years starting mid-calendar-year) |
| Policy independence | The same attendance and leave scenarios run against at least two deliberately different test policy sets | Proves no policy value is hard-coded |
| Security invariants | MFA invariants (no password-only session; last factor cannot be removed), elevation and break-glass limits, audit tamper detection (modify, delete, insert, drop partition, rewrite without key), Redis-down behaviour | Every commit |
| Migrations | Single Alembic head; `upgrade head → downgrade base → upgrade head` for application revisions; grants and catalog checks after upgrade; vendored Procrastinate SQL checksums | Every commit |
| Authorization matrix | Every endpoint × every role × {own, team, other, other-department} record | 100% of endpoints; generated from route metadata |
| API integration | Endpoints against a real PostgreSQL (testcontainers), including constraints and triggers | All MVP endpoints |
| Contract | Committed OpenAPI matches the app; frontend types regenerate cleanly | Every commit |
| Frontend component | Design-system components, forms, a11y checks | All design-system components |
| End-to-end | Critical journeys: invite → activate → MFA; clock in/break/out; correction request → approval; leave apply → approve → balance; document upload → download; deactivate user → session ends | Every release; smoke subset every commit |
| Security | See `security-architecture.md` §12 | — |
| Performance | Load test clock-in burst (all employees within 10 minutes) and approval queues at design load | Before MVP launch |

Coverage is a signal, not a goal. Domain and authorization code are expected to be close to fully covered; UI glue is covered by end-to-end tests.

Tests never use production data. Fixtures use obviously fake data.

## 8. Git and review

- Trunk-based: short-lived branches, PRs into `main`, squash merge.
- Conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`, `security:`).
- PR template includes: what changed, why, permissions affected, data classification affected, migration notes (expand/contract step, lock impact, staging duration), policy parameters added or changed, screenshots for UI (desktop + mobile), test evidence.
- CI must pass: lint, types, tests, import boundaries, OpenAPI contract, secret scan, dependency audit, banned-words check on UI copy.
- `main` is always deployable to staging.

## 9. Definition of done (per feature)

1. Backend: service, policies, permissions registered in the catalog, audit events, migrations, tests (unit + matrix + integration).
2. Frontend: routes gated by permission, loading/empty/error states, mobile layout, keyboard and screen-reader pass, axe clean.
3. Copy reviewed against writing rules.
4. Documentation updated where behaviour, permissions, schema or API changed (these docs are living documents).
5. No new warnings in lint/type checks. No leftover debug output, commented-out code, unused files or exports.
6. Demonstrated on staging with synthetic data.

## 10. Environments and configuration

- `APP_ENV` ∈ `local`, `test`, `staging`, `production`. Behaviour differences are explicit settings, not scattered `if env == ...` checks.
- Configuration via a single typed settings object (pydantic-settings) that fails fast on missing or invalid values.
- `.env.example` lists every variable with a comment and no real values.
- Production refuses to start with debug enabled, with default/weak keys, without TLS database connection, or with the seed command available.

## 11. Operations

- Every deployment follows `database-design.md` §11: one image digest, PITR confirmed, migration job as `hrms_migrator`, post-migration checks, then worker, then API, then smoke tests. The same digest must have deployed to staging first.
- Feature flags (settings-based, simple booleans) for staged rollout; removed once a feature is fully live.
- Incident basics: alert on error-rate spikes, job failures, audit chain mismatch, unusual download/export volume, login failure spikes.
- Runbooks in `docs/runbooks/` from Phase 1: restore from backup, rotate keys (including the audit checkpoint signing key), revoke all sessions for a user, respond to suspected account compromise, MFA reset identity check, break-glass use and review, total super admin lock-out recovery, audit chain mismatch response, Redis outage.
