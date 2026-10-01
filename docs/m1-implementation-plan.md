# Sigvitas HRMS — M1 (Phase 1) Implementation Plan

Status: In progress. Checkpoint A complete (2026-09-30); checkpoints B–H to do (§10, §12). Scope: `development-roadmap.md` M1 only. Nothing here changes the architecture; where the docs were ambiguous, the choice and its reason are stated in §10.

## 0. Environment found

| Item | Found | Needed |
|---|---|---|
| Git | Repo with one commit (docs), remote `github.com/darshanbr66/hrms` | — |
| Python | 3.12, 3.14 | 3.13 (ADR-004), managed per project by `uv` |
| uv | Not installed | Required (engineering-principles §3) |
| Node | 20.20.1 + corepack | OK for Vite 7 (Node ≥ 20.19) |
| pnpm | Not installed | Enable through corepack (pinned in `package.json`) |
| Docker | Not installed (Docker Desktop leftovers only) | Required: local services and testcontainers |
| PostgreSQL | Native 18.6 service on port 5432, listening on all interfaces | The project uses its own PostgreSQL 18 container on `127.0.0.1:5433`, so the native service is not touched |
| Redis | Not installed | Container |

## 1. Directory and file layout

```
hrms/
  .editorconfig  .gitattributes  .gitignore  .env.example  README.md
  .github/
    workflows/ci.yml                 # backend, frontend, contract, security jobs; runs on every push to main
  api/openapi.json                   # generated, committed, checked in CI
  infra/
    compose.yaml                     # postgres:18, redis:7.2, s3 emulator (object lock), mailpit
    db/bootstrap-roles.sql           # one-time cluster bootstrap: login roles + database (run by a DB admin)
    db/local-init.sh                 # local Compose only: runs bootstrap-roles.sql on first PostgreSQL start
    redis/start.sh  redis/ratelimit.acl  # Redis rate-limit store: no persistence, default user off, one ACL user
    docker/api.Dockerfile            # one image for api, worker, migration job, CLI
    docker/web.Dockerfile            # Caddy serving the SPA + reverse proxy to api (same origin)
    caddy/Caddyfile
  backend/
    pyproject.toml  uv.lock  .python-version (3.13)
    alembic.ini
    import-linter contracts          # in pyproject.toml ([tool.importlinter])
    migrations/
      env.py  script.py.mako  support.py  # support: helpers shared by revisions (quoting, role settings, grants)
      versions/0001_foundation.py … (see §3)
      vendor/procrastinate/<pinned-version>/*.sql + CHECKSUMS
    app/
      main.py                        # app factory, middleware, routers
      metadata.py                    # every module's models in one MetaData (Alembic, drift test)
      cli.py                         # bootstrap-super-admins, audit verify (ops)
      worker.py                      # Procrastinate worker entry point
      platform/
        config.py                    # pydantic-settings, fail-fast validation
        db.py                        # engines per role, session, unit of work
        clock.py                     # injectable clock
        errors.py                    # RFC 9457 problem details + handlers
        logging.py                   # JSON logs + redaction filter
        middleware.py                # request id, security headers, client IP (trusted proxies), CSRF/Origin
        ratelimit.py                 # Redis sliding window, HMAC-ed keys, fail-open/closed per scope
        jobs.py                      # Procrastinate app + periodic registrations
        email.py                     # outbox writer + SMTP sender (worker)
        idempotency.py               # Idempotency-Key handling
        storage.py                   # S3 client (anchor bucket in M1)
        security/
          passwords.py               # argon2-cffi, policy, breached check (HIBP k-anon + bundled list)
          tokens.py                  # random tokens, SHA-256 hashing, constant-time compare
          totp.py                    # pyotp wrapper, replay protection, QR (segno SVG)
          crypto.py                  # AES-256-GCM envelope encryption with key versions
          common_passwords.txt       # vendored list (source + licence in header)
        authz/
          catalog.py                 # permission catalog (source of truth), flags SU/R/phase
          roles.py                   # system role → permission matrix (authorization-model §4.1)
          engine.py                  # require(), scope_filter(), step-up, derived roles
          sod.py                     # SOD-1 … SOD-10
          context.py                 # Actor
          route.py                   # requires()/public_route() + route metadata
        audit/
          records.py                 # AuditEvent / SecurityEvent types and validation (no sensitive values)
          tables.py                  # audit_log + security_events table definitions
          writer.py                  # writers (same transaction / separate transaction for denials)
          partitions.py              # hourly audit.ensure_partitions job
          canonical.py               # RFC 8785 canonical row encoding, digest_version (F)
          sealer.py  checkpoints.py  verify.py  retention.py   (F)
    app/modules/
      identity/  (router.py schemas.py service.py repository.py models.py policies.py events.py public.py)
      access/    (same layout: roles, assignments, grant requests)
      org/       (models only in M1: locations, departments, designations; public.py when another module needs it)
      people/    (models, repository, public.py in M1: employees, employee_jobs, team resolution)
      audit/     (router for audit-log / security-events read APIs used by tests; UI in M5)
      settings/  (typed setting registry, read/update API)
    scripts/export_openapi.py        # regenerates api/openapi.json
    tests/
      unit/ … integration/ … security/ (matrix, sod, mfa invariants, tamper, redis-down) … conftest.py
  frontend/
    package.json  pnpm-lock.yaml  vite.config.ts  tsconfig*.json  eslint.config.js  .prettierrc
    index.html
    src/
      main.tsx
      app/        (providers, router, error boundary, session bootstrap, step-up provider)
      design-system/ (tokens.css, Button, IconButton, TextField, Form error summary, Dialog, AlertDialog,
                      Toast, Table, EmptyState, ErrorState, Skeleton, PageHeader, Banner, StatusPill)
      features/
        auth/       (sign-in, verify, invite activation, re-enrolment, password reset)
        account/    (security page: sessions, login history, MFA factors, recovery codes, password)
        admin/      (users, user detail: roles, sessions, MFA reset, disable; access requests; break-glass)
      lib/        (api client via openapi-fetch + generated types, problem-details mapping, formatting)
    tests/e2e/    (Playwright)
```

## 2. Database tables in M1

Columns follow `database-design.md` §4 exactly unless noted.

| Schema | Tables in M1 | Note |
|---|---|---|
| `org` | locations, departments, designations | Tables only, no API/UI (M2). Needed by `people.employee_jobs` FKs. |
| `people` | employees, employee_jobs | Tables + team resolution in `people.public` (`team_member_ids_query`, `is_team_member`, `has_direct_reports`), recursive CTEs as of a date. No personal/sensitive tables (M2). |
| `identity` | users, credentials, mfa_factors, recovery_codes, sessions, session_tokens, one_time_tokens, trusted_devices | Full. Enrolment-only sessions: `sessions.scope` (`full`, `mfa_enrolment`) — the column the docs imply for "enrolment-only session". |
| `access` | permissions, roles, role_permissions, user_roles, role_grant_requests | Full, with catalog + system roles as data revision. |
| `audit` | audit_log, security_events (partitioned monthly on `recorded_at`), chain_links (partitioned), chain_checkpoints | Full integrity design. The two streams and their writer come in B; the chain tables with the sealer in F. |
| `notify` | email_outbox | Only the outbox (emails for invite, reset, re-enrolment, lockout, new device, elevation). In-app notifications are M5. |
| `app` | settings, idempotency_keys | `export_jobs` is M5. |
| Procrastinate | vendored schema of the pinned release | Applied by Alembic. |

Technical setting keys seeded (values are the security defaults already written in `security-architecture.md`, not HR policy): session idle/absolute timeouts, access-token TTL, step-up window, invite/reset/re-enrolment token TTLs, lockout thresholds, elevation default/max duration, HIBP enabled flag. No HR values are seeded.

## 3. Migration sequence (single linear Alembic history)

| Rev | Contents |
|---|---|
| 0001 | Extensions (`citext`, `btree_gist`, `pg_trgm`), schemas, default privileges for `hrms_app`/`hrms_worker`/`hrms_audit_retention`, role session settings (`statement_timeout`, `transaction_timeout`, `idle_in_transaction_session_timeout`) |
| 0002 | Procrastinate base schema (vendored SQL, checksum-verified) + grants on its objects |
| 0003 | `org` tables (B) |
| 0004 | `people.employees`, `people.employee_jobs` (+ exclusion constraint, manager index, job-history guard) (B) |
| 0005 | `audit.audit_log`, `audit.security_events`, append-only and `recorded_at` triggers, `audit.ensure_partitions`, initial monthly partitions (current + 3 ahead), audit grants (B) |
| later | `identity` tables |
| later | `access` tables + CHECK constraints (SOD-3 in DB, grant request limits); `user_roles` references `identity.users`, so this follows the identity revision |
| later | Data: permission catalog + system roles + role_permissions (generated from `authz/catalog.py` and `authz/roles.py`) |
| later | `notify.email_outbox`, `app.settings`, `app.idempotency_keys`; seed technical setting keys |
| later | `audit.chain_links`, `audit.chain_checkpoints` and the retention role's grants (with the sealer, F) |

Revisions after 0005 are numbered in the order they are written, following their foreign-key dependencies.

`infra/db/bootstrap-roles.sql` (not Alembic, run once per environment by a DB admin): creates login roles `hrms_migrator` (CREATEROLE, owns the database), `hrms_app`, `hrms_worker`, `hrms_audit_retention`, grants `hrms_migrator` ADMIN OPTION on them so migrations can set their session defaults. Passwords are passed as psql variables from the secret store, never committed.

## 4. API endpoints in M1 (`/api/v1`)

**Public (allow-listed):** `POST /auth/login`, `POST /auth/login/mfa`, `POST /auth/refresh`, `GET /auth/invite/{token}`, `POST /auth/invite/{token}/password`, `POST /auth/invite/{token}/mfa/totp/setup`, `POST /auth/invite/{token}/mfa/totp/confirm`, `POST /auth/mfa-reenrolment/{token}`, `POST /auth/password-reset`, `POST /auth/password-reset/{token}`, `GET /api/health/live`, `GET /api/health/ready`.

**Authenticated self:** `POST /auth/logout`, `POST /auth/step-up`, `GET /me`, `POST /me/password`, `GET /me/mfa/factors`, `POST /me/mfa/totp/setup`, `POST /me/mfa/totp/confirm`, `DELETE /me/mfa/factors/{id}`, `POST /me/mfa/recovery-codes`, `GET /me/sessions`, `DELETE /me/sessions/{id}`, `GET /me/login-history`.

**Administration (permission-gated):**
- Users: `GET /users` (user.read.all), `POST /users` — create an invited account for an email, optionally linked to an existing employee (user.invite), `POST /users/{id}/invite` resend, `POST /users/{id}/disable` · `/enable` (user.disable), `POST /users/{id}/mfa-reset` (security.mfa.reset), `GET/DELETE /users/{id}/sessions` (auth.session.*.all).
- Roles: `GET /roles`, `GET /roles/{id}`, `GET /users/{id}/roles` (role.read), `POST /users/{id}/roles`, `DELETE /users/{id}/roles/{assignment_id}` (role.assign; `super_admin` → 202 + request).
- Grant requests: `GET /role-grant-requests`, `POST /role-grant-requests`, `POST /role-grant-requests/{id}/approve` · `/reject` · `/revoke`, `POST /role-grant-requests/break-glass`, `POST /role-grant-requests/{id}/acknowledge`.
- Audit read: `GET /audit-log`, `GET /security-events` (audit.read / security.event.read) — API only in M1; viewers are M5.
- Settings: `GET /settings` (settings.read), `PUT /settings/{key}` (settings.manage).

## 5. Frontend screens in M1

| Route | Screen |
|---|---|
| `/sign-in`, `/sign-in/verify` | Two-step sign-in; verify accepts TOTP or "use a recovery code" |
| `/invite/:token` | Activation: password → authenticator → recovery codes |
| `/mfa-reenrolment/:token` | Re-enrolment after admin reset |
| `/password-reset`, `/password-reset/:token` | Request and complete reset |
| `/account/security` | Landing page in M1: sessions, login history, MFA factors, recovery codes, change password |
| `/admin/users`, `/admin/users/:id` | Invite, list, disable/enable, MFA reset, sessions, role assignments (shown only with permissions) |
| `/admin/access-requests` | Elevation requests, approvals, active elevations, break-glass + acknowledgement |
| Global | App shell (sidebar / mobile bottom nav with only permitted items), step-up dialog, elevation banner, enrolment-only mode |

There is no Today/home page with invented content. `/` redirects to `/account/security` until M3 adds Today.

## 6. Test strategy

| Layer | M1 content |
|---|---|
| Unit | password policy, token hashing, TOTP (RFC 6238 vectors, replay), envelope crypto, canonical encoding, authz engine (every permission × scope), SoD 1–10 allowed/blocked, rate-limit window math, settings validation |
| Integration (real PostgreSQL 18 + Redis via testcontainers) | every endpoint; migrations up/down/up; grants (app cannot UPDATE/DELETE audit; retention role limits); constraints (SOD-3 check, grant request limits, exclusion constraints); lockout; refresh rotation + reuse detection; enrolment-only restrictions |
| Security | route coverage (every route has permission or is public-allow-listed); authorization matrix generated from route metadata × roles × own/other; MFA invariants (no password-only session anywhere; last factor protected); elevation/break-glass limits and expiry without a job; audit tamper suite (modify, delete, insert, reorder, truncate tail, drop partition, rewrite chain without key, delete checkpoint, retention with and without tombstone); Redis-down behaviour per scope; log redaction (fixture secrets never in logs) |
| Contract | OpenAPI regenerated = committed; frontend types compile |
| Frontend | component tests + axe for design-system components and forms |
| E2E (Playwright, local compose) | bootstrap → activate with MFA → sign in → sessions revoke; recovery-code sign-in → re-enrol; elevation request → approval → expiry; step-up prompt |

## 7. Local services (Docker Compose, all bound to 127.0.0.1)

| Service | Image | Port |
|---|---|---|
| PostgreSQL 18 | `postgres:18` | 5433 |
| Redis | `redis:7.2` (ADR-010; 7.2 is the maintained Redis 7 line under the BSD-3 licence, 7.4 and later are RSALv2/SSPLv1 or AGPLv3) — persistence off, ACL user | 6380 |
| S3 emulator with object lock | chosen at step 2 after checking current licensing/distribution (candidates: MinIO, SeaweedFS, LocalStack); must support Object Lock compliance mode | 9000 |
| Mail catcher | `axllent/mailpit` | 1025 / 8025 |

ClamAV is not added in M1 because nothing uploads files until M5. Adding it now would be an unused service.

## 8. Environment variables (`.env.example`, no values)

`APP_ENV`, `APP_BASE_URL`, `LOG_LEVEL`, `TRUSTED_PROXY_CIDRS`,
`DATABASE_URL_APP`, `DATABASE_URL_WORKER`, `DATABASE_URL_MIGRATOR`, `DATABASE_URL_AUDIT_RETENTION`,
`REDIS_URL`, `RATE_LIMIT_KEY_HMAC_KEY`,
`FIELD_ENCRYPTION_KEYS` (JSON map version → base64 key; KEK source in production is the secret manager), `FIELD_ENCRYPTION_ACTIVE_VERSION`,
`AUDIT_SIGNING_PRIVATE_KEY` (worker only), `AUDIT_SIGNING_KEY_ID`, `AUDIT_VERIFY_PUBLIC_KEYS` (JSON key_id → public key),
`S3_ENDPOINT_URL`, `S3_REGION`, `AUDIT_ANCHOR_BUCKET`, `AUDIT_ANCHOR_ACCESS_KEY_ID`, `AUDIT_ANCHOR_SECRET_ACCESS_KEY`,
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_SECURITY` (`starttls` | `tls` | `none` — `none` refused outside `local`/`test`),
`HIBP_ENABLED`.

Startup refuses to run in `staging`/`production` with missing keys, debug on, non-TLS database URLs, or `SMTP_SECURITY=none`.

## 9. Dependencies (each with its reason)

**Backend runtime:** fastapi, uvicorn[standard], pydantic, pydantic-settings, email-validator (EmailStr), sqlalchemy[asyncio], psycopg[binary,pool], alembic, procrastinate, argon2-cffi (Argon2id), pyotp (TOTP, well-reviewed), segno (QR as SVG, no deps), cryptography (AES-GCM, Ed25519), rfc8785 (canonical JSON for audit digests), redis (async client), httpx (HIBP range API), boto3 (S3 object-lock anchoring).
**Backend dev:** pytest, pytest-asyncio, testcontainers, ruff, mypy, import-linter, pip-audit.
**Frontend runtime:** react, react-dom, react-router, @tanstack/react-query, react-hook-form, zod, @hookform/resolvers, react-aria-components, lucide-react, openapi-fetch (typed client over generated types), @fontsource/ibm-plex-sans.
**Frontend dev:** vite, @vitejs/plugin-react, typescript, tailwindcss, @tailwindcss/vite, openapi-typescript, eslint + typescript-eslint + eslint-plugin-react-hooks + eslint-plugin-jsx-a11y, prettier, vitest, @testing-library/react, jsdom, @playwright/test, @axe-core/playwright, vitest-axe.
**Deliberately not in M1:** TanStack Table and Motion (no screen needs them yet), ClamAV, any UI kit.

## 10. Order of work and checkpoints

The requested order is kept except where a later step is a hard dependency of an earlier one. Each checkpoint ends with tests, lint and types, and a report.

| Checkpoint | Steps | Why this position |
|---|---|---|
| A. Foundation | 1 repo, 25 CI skeleton, 2 compose, 3 PostgreSQL config, 5 Alembic, 6 Procrastinate, 7 roles/grants/timeouts, 4 Redis rate limiter | CI from the first commit so every later step is gated |
| B. Audit writer + minimal org/people | 19, 22 | Identity must audit from its first line; `users.employee_id` needs `people.employees` |
| C. Authorization engine | 16, 17 | Admin identity flows (MFA reset, disable, invite) need `require()` |
| D. Identity | 8–14 | Models, invite, password, mandatory MFA, two-step sign-in, recovery codes, step-up |
| E. Admin flows | 15, 18, 24 | MFA reset/re-enrolment, grant requests, bootstrap CLI |
| F. Audit integrity | 20, 21 | Sealer, checkpoints, anchors, verification, retention, tamper suite |
| G. Frontend | 23 | Screens over a finished, tested API |
| H. Delivery | 26, 27 | Needs hosting decision (ADR-023) |

## 11. Small documentation corrections to make during M1

1. Roadmap M1 exit says a super admin "invites a user", but `super_admin` does not hold `user.invite` (authorization-model §4.1). Correct the wording to: the super admin invites through an approved elevation to `system_admin`. This keeps the least-privilege model and exercises elevation in the exit test.
2. Security architecture names `argon2-cffi (through pwdlib)`. Use `argon2-cffi` directly (it already provides hashing, verification and rehash detection), which removes a dependency.
3. Add `sessions.scope` and `POST /users` (create invited account without an employee record until M2) to database-design and api-architecture.

## 12. Progress

### Checkpoint A — foundation (done, 2026-09-30)

Delivered: repository hygiene files, CI (`.github/workflows/ci.yml`: Ruff, mypy strict, pytest against real PostgreSQL 18 and Redis, pip-audit, gitleaks, Semgrep; actions pinned by commit SHA), Compose (PostgreSQL 18.6, Redis 7.2.16 with ACL, running as the non-root `redis` user in Compose and in the test containers, checked by `test_server_does_not_run_as_root`), cluster bootstrap SQL, typed settings per process, database engine and unit of work, problem details, JSON logging with redaction, request middleware (request ID, security headers, trusted-proxy client address, route-template access log), health endpoints, Alembic (`env.py` runs only as `hrms_migrator`, sets `lock_timeout`, refuses downgrades in staging/production), revisions 0001 (extensions, schemas, default privileges, role limits) and 0002 (Procrastinate 3.10.0 base schema, checksum-verified), Procrastinate app and worker entry point, and the Redis rate limiter (sliding log, HMAC-ed keys, fail-closed/fail-open per scope).

Decisions taken during the checkpoint (documents updated in the same change):

- Procrastinate objects live in a dedicated `procrastinate` schema; runtime roles get `search_path = procrastinate, public` (`database-design.md` §11).
- Rate-limit counters are sliding logs so that hourly limits still keep every key's TTL at or under one hour; every subject, including user IDs, is HMAC-ed (`architecture.md` §3.1).
- Problem `type` values are relative (`/problems/<name>`) so they are identical across environments (`api-architecture.md` §4).
- mypy strict is the type checker (`engineering-principles.md` §3).
- SQLAlchemy 2.1 (current release of the 2.x line) is used.
- On Windows, the API runs with `--loop asyncio:SelectorEventLoop` and the worker selects the selector loop itself, because psycopg's async driver does not support the Proactor loop.

Moved to the checkpoint that first needs them, so nothing unused is shipped: the mail catcher (D), the S3 emulator with object lock (F), import-linter contracts (delivered in B), the banned-words check on UI copy and the frontend CI job (G).

Open: a second engineer to review authentication, authorization, payroll, documents and audit code (`engineering-principles.md` §8). Until one is available, the project owner authorizes adversarial self-review per checkpoint; the human review of that code is still owed.

### Checkpoint B — audit writer and minimal org/people (done, 2026-10-01)

Delivered: revisions 0003 (`org` tables), 0004 (`people.employees`, `people.employee_jobs` with the exclusion constraint and job-history guard) and 0005 (`audit.audit_log` and `audit.security_events` partitioned by UTC month, append-only and `recorded_at` triggers, `audit.ensure_partitions`, initial partitions, grants); SQLAlchemy models and `app/metadata.py`; the audit writer (`platform/audit`: validated record types, same-transaction and separate-transaction writes); the hourly `audit.ensure_partitions` worker job; team resolution in `people.public`; import-linter contracts in CI. No endpoints, so the OpenAPI contract is unchanged.

Decisions taken during the checkpoint (documents updated in the same change):

- Team resolution uses the job rows that apply on a date, not open-ended rows, so future-dated changes do not move team access early; it counts only employees employed on that date (`authorization-model.md` §7, `database-design.md` §4.4).
- Job history is guarded in the database: only `effective_to` may change; delete and truncate are refused (`database-design.md` §4.4, §6).
- Audit rows get their `id` and `recorded_at` from the database only, enforced by a trigger (a supplied `id` is replaced, so the Checkpoint F sealer can key chain links by record ID); the primary key is `(id, recorded_at)`; audit tables have no foreign keys; month partitions are created by a worker-only `SECURITY DEFINER` function, detached then attached (`database-design.md` §3, §4.10).
- `security_events` also records `session_id` and `request_id`; severities are `info`, `warning`, `high` (`database-design.md` §4.10).
- The chain tables move to Checkpoint F with the sealer that writes them; directory-search indexes move to M2 with the query that uses them.
- A schema drift test compares the models with the migrated database on every run.
- The audit record types refuse values for personal field names (`security-architecture.md` §2) as well as secret ones, refuse contact details and email-shaped values in security event details, and refuse NUL characters.

Deferred with reason: the keyed hash for `email_attempted_hash` is computed by the sign-in flow (D), which also introduces its dedicated key; the writer accepts only a 32-byte digest.

Review (2026-10-01): no second engineer was available, so the project owner authorized an adversarial self-review (`engineering-principles.md` §8). No human has reviewed this checkpoint yet; that review is still owed. Defects found and fixed, each with a regression test:

- Audit rows accepted a caller-supplied `id`, which could duplicate across partitions and later stall the sealer; the insert trigger now always assigns it.
- Audit changes accepted values for personal field names, and security event details accepted email or phone keys and email-shaped values; both are now refused.
- NUL characters reached PostgreSQL as unclear errors; they are now refused when the record is built.
- `audit.ensure_partitions` skipped a same-named table that was not attached, so the job reported success while the month had no partition; it now fails loudly.
- The back-dating test passed because the earlier month had no partition, not because of the trigger; it now targets existing partitions and checks the trigger's message.
- `security-architecture.md` §8 wrongly said runtime roles have no privileges on partitions (they read them through the schema's default grant; they cannot insert into them).

Automated verification at commit time: `uv sync --locked`; pytest 269 passed (including the migration gates: single head `0005`, upgrade/downgrade/upgrade, schema drift); Ruff format and lint clean; mypy strict clean; import-linter 3 contracts kept (a planted violation was reported, then removed); pip-audit `--strict` no known vulnerabilities; gitleaks v8.30.1 and Semgrep 1.178.0 (CI's rule sets) no findings; OpenAPI regenerated unchanged; Compose configuration valid.

Future decisions, not made here: rehire (one `employees` row holds one joining and exit date); rules for correcting `effective_to` on historical job rows (the database currently allows any `effective_to` change, M2 write path); connection-pool behaviour of `record_separately` on denial paths under load (C/D).
