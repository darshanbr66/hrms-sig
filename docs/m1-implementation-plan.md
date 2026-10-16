# Sigvitas HRMS — M1 (Phase 1) Implementation Plan

Status: In progress. Checkpoints A, B, C and D complete (2026-10-01; D verified again 2026-10-05); E–H to do (§10, §12). Scope: `development-roadmap.md` M1 only. Nothing here changes the architecture; where the docs were ambiguous, the choice and its reason are stated in §10.

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
| `notify` | email_outbox | Only the outbox (emails for invite, reset, lockout, new device, password and MFA changes in D; re-enrolment and elevation in E). In-app notifications are M5. |
| `app` | settings, idempotency_keys | `export_jobs` is M5. `idempotency_keys` arrives with the first endpoint that takes an `Idempotency-Key` header. |
| Procrastinate | vendored schema of the pinned release | Applied by Alembic. |

Technical settings (D): declared in the code registry with the security defaults already written in `security-architecture.md` as defaults; nothing is seeded, the table holds only overrides (`database-design.md` §4.11). Session timeouts, invite/reset TTLs, lockout thresholds, the per-IP sign-in threshold, the trusted-device lifetime and email delivery attempts are settings. The access-token TTL and step-up window stay fixed in code (they are security properties, not tuning); elevation durations come with E; the HIBP flag stays an environment variable. No HR values exist as settings.

## 3. Migration sequence (single linear Alembic history)

| Rev | Contents |
|---|---|
| 0001 | Extensions (`citext`, `btree_gist`, `pg_trgm`), schemas, default privileges for `hrms_app`/`hrms_worker`/`hrms_audit_retention`, role session settings (`statement_timeout`, `transaction_timeout`, `idle_in_transaction_session_timeout`) |
| 0002 | Procrastinate base schema (vendored SQL, checksum-verified) + grants on its objects |
| 0003 | `org` tables (B) |
| 0004 | `people.employees`, `people.employee_jobs` (+ exclusion constraint, manager index, job-history guard) (B) |
| 0005 | `audit.audit_log`, `audit.security_events`, append-only and `recorded_at` triggers, `audit.ensure_partitions`, initial monthly partitions (current + 3 ahead), audit grants (B) |
| 0006 | Accept two-part permission keys in `audit_log.permission_used` (C; corrects 0005) |
| 0007 | `identity` tables (users, credentials, mfa_factors, recovery_codes, sessions, session_tokens, one_time_tokens) + the MFA invariant trigger (C) |
| 0008 | `access` tables (permissions, roles, role_permissions, user_roles) + SOD-3 check, derived-role foreign key, assignment history trigger, read-only catalog grants (C) |
| 0009 | Data: permission catalog + system roles + role_permissions (a frozen copy of `authz/catalog.py` and `authz/roles.py`) (C) |
| 0010 | `notify.email_outbox`, `identity.trusted_devices`, `app.settings`; `password_reset` added to the one-time token purposes and session revocation reasons (D) |
| 0011 | `audit.ensure_partitions` also fails for a partition attached for the wrong range and for a default partition (D verification; corrects 0005) |
| 0012 | `identity.users.mfa_reenrolment_required` (MFA invariant widened, full sessions refused while re-enrolment is pending), `mfa_reenrolment` token purpose, `mfa_reset` revocation reason, outbox templates `identity.mfa_reset` and `access.grant_request`; `access.role_grant_requests` with its history trigger; `user_roles.grant_request_id` and the insert trigger matching an assignment to its request (E) |
| 0013 | `audit.chain_links` (partitioned by stream, then month), `audit.chain_checkpoints` with the sequence trigger, append-only triggers, `audit.ensure_partitions` creating chain partitions, `audit.drop_expired_partition` (retention role only), worker and retention grants, the `audit.chain_mismatch` outbox template (F) |
| later | `app.idempotency_keys` (first idempotent endpoint) |

Revisions after 0005 are numbered in the order they are written, following their foreign-key dependencies.

`infra/db/bootstrap-roles.sql` (not Alembic, run once per environment by a DB admin): creates login roles `hrms_migrator` (CREATEROLE, owns the database), `hrms_app`, `hrms_worker`, `hrms_audit_retention`, grants `hrms_migrator` ADMIN OPTION on them so migrations can set their session defaults. Passwords are passed as psql variables from the secret store, never committed.

## 4. API endpoints in M1 (`/api/v1`)

**Public (allow-listed):** `POST /auth/login`, `POST /auth/login/mfa`, `POST /auth/refresh`, `GET /auth/invite/{token}`, `POST /auth/invite/{token}/password`, `POST /auth/invite/{token}/mfa/totp/setup`, `POST /auth/invite/{token}/mfa/totp/confirm`, `POST /auth/mfa-reenrolment/{token}`, `POST /auth/password-reset`, `POST /auth/password-reset/{token}` (D), `GET /api/health/live`, `GET /api/health/ready`.

**Authenticated self:** `POST /auth/logout`, `POST /auth/step-up`, `GET /me`, `POST /me/password`, `GET /me/mfa/factors`, `POST /me/mfa/totp/setup`, `POST /me/mfa/totp/confirm`, `DELETE /me/mfa/factors/{id}`, `POST /me/mfa/recovery-codes`, `GET /me/sessions`, `DELETE /me/sessions/{id}`, `GET /me/login-history`, `GET /me/devices`, `DELETE /me/devices/{id}` (D).

**Administration (permission-gated):**
- Users: `GET /users` (user.read.all), `POST /users` — create an invited account for an email, optionally linked to an existing employee (user.invite, plus employee.lifecycle.manage over that employee), `POST /users/{id}/invite` resend, `DELETE /users/{id}/invite` revoke (D), `POST /users/{id}/disable` · `/enable` (user.disable), `POST /users/{id}/mfa-reset` (security.mfa.reset), `GET/DELETE /users/{id}/sessions` (auth.session.*.all).
- Roles: `GET /roles`, `GET /roles/{id}`, `GET /users/{id}/roles` (role.read), `POST /users/{id}/roles`, `DELETE /users/{id}/roles/{assignment_id}` (role.assign; `super_admin` → 202 + request).
- Grant requests: `GET /role-grant-requests`, `POST /role-grant-requests`, `POST /role-grant-requests/{id}/approve` · `/reject` · `/revoke`, `POST /role-grant-requests/break-glass`, `POST /role-grant-requests/{id}/acknowledge`.
- Audit read: `GET /audit-log`, `GET /security-events` (audit.read / security.event.read) — API only in M1; viewers are M5.
- Settings: `GET /settings` (settings.read), `PUT /settings/{key}` (settings.manage; `security.*` keys also security.settings.manage) (D).

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
| S3 emulator with object lock | `rustfs/rustfs:1.0.0` pinned by digest (Apache-2.0), chosen in F after testing Object Lock compliance mode: RustFS refused deleting a locked version (even as root), shortening its retention and switching it to governance; Versity Gateway allowed shortening a compliance retention and was rejected | 9000 |
| Mail catcher | `axllent/mailpit` | 1025 / 8025 |

ClamAV is not added in M1 because nothing uploads files until M5. Adding it now would be an unused service.

## 8. Environment variables (`.env.example`, no values)

`APP_ENV`, `APP_BASE_URL`, `LOG_LEVEL`, `TRUSTED_PROXY_CIDRS`,
`DATABASE_URL_APP`, `DATABASE_URL_WORKER`, `DATABASE_URL_MIGRATOR`, `DATABASE_URL_AUDIT_RETENTION`,
`REDIS_URL`, `RATE_LIMIT_KEY_HMAC_KEY`,
`FIELD_ENCRYPTION_KEYS` (JSON map version → base64 key; KEK source in production is the secret manager), `FIELD_ENCRYPTION_ACTIVE_VERSION`,
`AUDIT_SIGNING_PRIVATE_KEY` (worker only), `AUDIT_SIGNING_KEY_ID`, `AUDIT_VERIFY_PUBLIC_KEYS` (JSON key_id → public key),
`S3_ENDPOINT_URL`, `S3_REGION`, `AUDIT_ANCHOR_BUCKET`, `AUDIT_ANCHOR_ACCESS_KEY_ID`, `AUDIT_ANCHOR_SECRET_ACCESS_KEY`,
`AUDIT_LOG_RETENTION_MONTHS`, `SECURITY_EVENTS_RETENTION_MONTHS` (optional; unset means no audit partition is removed),
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_SECURITY` (`starttls` | `tls` | `none` — `none` refused outside `local`/`test`),
`HIBP_ENABLED`.

Startup refuses to run in `staging`/`production` with missing keys, debug on, non-TLS database URLs, or `SMTP_SECURITY=none`.

## 9. Dependencies (each with its reason)

**Backend runtime:** fastapi, uvicorn[standard], pydantic, pydantic-settings, email-validator (EmailStr), sqlalchemy[asyncio], psycopg[binary,pool], alembic, procrastinate, argon2-cffi (Argon2id), pyotp (TOTP, well-reviewed), segno (QR as SVG, no deps), cryptography (AES-GCM, Ed25519), rfc8785 (canonical JSON for audit digests), redis (async client), httpx (HIBP range API), boto3 (S3 object-lock anchoring).
**Backend dev:** pytest, pytest-asyncio, testcontainers, ruff, mypy, import-linter, pip-audit.
**Frontend runtime:** react, react-dom, react-router, @tanstack/react-query, react-hook-form, zod, @hookform/resolvers, react-aria-components, lucide-react, openapi-fetch (typed client over generated types), @fontsource/ibm-plex-sans, @fontsource/ibm-plex-mono (2026-10-16: codes and IDs, as `ui-ux-guidelines.md` §4 specifies), @internationalized/date (3.12.4, pinned, 2026-10-16: the date objects React Aria's Calendar needs for the attendance month view; already installed as a dependency of react-aria-components).
**Frontend dev:** vite, @vitejs/plugin-react, typescript (6.x: openapi-typescript and typescript-eslint need its JavaScript compiler API, which TypeScript 7 does not ship), tailwindcss, @tailwindcss/vite, openapi-typescript, eslint + @eslint/js + globals + typescript-eslint + eslint-plugin-react-hooks + eslint-plugin-jsx-a11y, prettier, vitest, @testing-library/react, @testing-library/user-event (realistic typing and clicking in tests), @testing-library/jest-dom (DOM assertions), jsdom (26.x, which runs on Node 20), vitest-axe; @playwright/test (1.60.0, added 2026-10-16 for browser layout tests that jsdom cannot make: dialog fit, scrolling, viewport sizes; `pnpm test:e2e`) and @axe-core/playwright (4.13.0, pinned, added 2026-10-16: WCAG checks, colour contrast included, in both themes in a real browser, which jsdom cannot compute).
**Deliberately not in M1:** TanStack Table and Motion (no screen needs them yet), ClamAV, any UI kit.

## 10. Order of work and checkpoints

The requested order is kept except where a later step is a hard dependency of an earlier one. Each checkpoint ends with tests, lint and types, and a report.

| Checkpoint | Steps | Why this position |
|---|---|---|
| A. Foundation | 1 repo, 25 CI skeleton, 2 compose, 3 PostgreSQL config, 5 Alembic, 6 Procrastinate, 7 roles/grants/timeouts, 4 Redis rate limiter | CI from the first commit so every later step is gated |
| B. Audit writer + minimal org/people | 19, 22 | Identity must audit from its first line; `users.employee_id` needs `people.employees` |
| C. Identity, authentication, sessions and authorization | 16, 17, 8, 10–14, part of 23 | Re-scoped by the project owner (2026-10-01): the engine plus sign-in, sessions, MFA, step-up and the frontend sign-in shell, so identity and authorization land and are tested together |
| D. Email-based identity flows and settings | 9, rest of 11, 24 (outbox) | Email outbox and SMTP, admin invites (`POST /users`), password reset, new-device and lockout emails, keyed email hash, the settings registry |
| E. Admin flows | 15, 18, 24 | MFA reset/re-enrolment, grant requests, bootstrap CLI |
| F. Audit integrity | 20, 21 | Sealer, checkpoints, anchors, verification, retention, tamper suite |
| G. Frontend | 23 | Screens over a finished, tested API |
| H. Delivery | 26, 27 | Needs hosting decision (ADR-023) |

## 11. Small documentation corrections to make during M1

Status: 2 is done (C uses `argon2-cffi` directly); 3 is done (`sessions.scope` in C, `POST /users` in D).

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

Moved to the checkpoint that first needs them, so nothing unused is shipped: the mail catcher (D, delivered), the S3 emulator with object lock (F), import-linter contracts (delivered in B), the banned-words check on UI copy and the frontend CI job (G).

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

Deferred with reason: the keyed hash for `email_attempted_hash` is computed by the sign-in flow (D), which also introduces its dedicated key; the writer accepts only a 32-byte digest. (Delivered in D.)

Review (2026-10-01): no second engineer was available, so the project owner authorized an adversarial self-review (`engineering-principles.md` §8). No human has reviewed this checkpoint yet; that review is still owed. Defects found and fixed, each with a regression test:

- Audit rows accepted a caller-supplied `id`, which could duplicate across partitions and later stall the sealer; the insert trigger now always assigns it.
- Audit changes accepted values for personal field names, and security event details accepted email or phone keys and email-shaped values; both are now refused.
- NUL characters reached PostgreSQL as unclear errors; they are now refused when the record is built.
- `audit.ensure_partitions` skipped a same-named table that was not attached, so the job reported success while the month had no partition; it now fails loudly.
- The back-dating test passed because the earlier month had no partition, not because of the trigger; it now targets existing partitions and checks the trigger's message.
- `security-architecture.md` §8 wrongly said runtime roles have no privileges on partitions (they read them through the schema's default grant; they cannot insert into them).

Automated verification at commit time: `uv sync --locked`; pytest 269 passed (including the migration gates: single head `0005`, upgrade/downgrade/upgrade, schema drift); Ruff format and lint clean; mypy strict clean; import-linter 3 contracts kept (a planted violation was reported, then removed); pip-audit `--strict` no known vulnerabilities; gitleaks v8.30.1 and Semgrep 1.178.0 (CI's rule sets) no findings; OpenAPI regenerated unchanged; Compose configuration valid.

Future decisions, not made here: rehire (one `employees` row holds one joining and exit date); rules for correcting `effective_to` on historical job rows (the database currently allows any `effective_to` change, M2 write path); connection-pool behaviour of `record_separately` on denial paths under load (C/D).

### Checkpoint C — identity, authentication, sessions and authorization (done, 2026-10-01)

Scope set by the project owner: the authorization engine (plan C) together with sign-in, sessions, MFA and step-up (most of plan D), and the frontend sign-in shell (part of G). Flows that need outbound email moved to D.

Delivered:

- Revisions 0006 (two-part permission keys in `audit_log.permission_used`, correcting 0005), 0007 (identity tables and the MFA invariant trigger), 0008 (access tables, SOD-3 check, derived-role foreign key, assignment history trigger, read-only catalog grants) and 0009 (catalog and system roles seed).
- `platform/security`: Argon2id hashing and policy with the bundled breached-password list and the optional range API, 256-bit tokens stored as SHA-256 digests, 80-bit recovery codes, TOTP with replay protection, AES-256-GCM field encryption with key versions.
- `platform/authz`: the permission catalog (MVP permissions), the system role matrix, the engine (`require`, `scope_filter`, step-up, department/location constraints, team scope as of a date), separation-of-duties checks, and route declarations (`requires`, `account`, `public_route`) with allow-lists. CSRF middleware (custom header + Origin).
- `modules/identity`: two-step sign-in, account lockout and per-IP throttling, server-side sessions with single-use refresh rotation and reuse detection, sign-out, own sessions and sign-in history, step-up, password change, MFA factors (add, confirm, remove with the last-factor guard), recovery codes, invite activation (password, authenticator, recovery codes), account administration (list, disable/enable, sessions of others).
- `modules/access`: per-request actor resolution (assignments, derived `employee`/`manager`, account baseline), roles and role assignment with SOD-3, SOD-8 (refusal until grant requests), SOD-10 and the two-super-admin minimum, rotation of the target's sessions.
- `modules/audit`: audited reads of the audit log and security events.
- `app/cli.py bootstrap-super-admins`: the first two super admins as invited accounts, printed invite links.
- Frontend (`frontend/`): Vite, React 19, TypeScript strict, Tailwind v4 tokens, React Aria components, TanStack Query, React Hook Form + Zod, typed API client generated from `api/openapi.json` with single-flight refresh; sign-in (two steps, recovery code), invite activation, account security (sessions, authenticators, recovery codes, password, sign-in history), enrolment-only mode, step-up dialog, access-denied and not-found states, lazy-loaded feature routes; CI job.

Decisions (documents updated in the same change):

- Every active account holds the account baseline (own sessions and sign-in history); the account's own security routes need no permission and are on an account allow-list (`authorization-model.md` §4.1, `security-architecture.md` §12).
- One `invalid-credentials` problem type for every sign-in failure (`api-architecture.md` §4).
- Assigning `super_admin` is refused until the grant-request approval exists (E); the bootstrap command is the only source of super admins until then. Removing one that would leave fewer than two is refused (`security-architecture.md` §9).
- A role change ends the user's access tokens; the next refresh issues new ones (session rotation without signing the user out).
- "Today" for authorization is the UTC date until location time zones (M2).
- Locked reads always refresh the row (`populate_existing`), and session changes lock account, session, then tokens.
- Password hashing is bounded to 4 concurrent hashes per API process.
- TypeScript is pinned to 6.x and jsdom to 26.x for tool and Node 20 compatibility.

Deferred, with where they go: email outbox, admin invites (`POST /users`), password reset, new-device and lockout emails, `trusted_devices` and the keyed attempted-email hash, the settings registry (D, all delivered in D); MFA reset and re-enrolment, grant requests, elevation and break-glass, `super_admin` assignment (E); retention purge of expired tokens and sessions (F/M5); mobile token transport (`X-Client-Type`) with the mobile client; Playwright end-to-end tests and a real-browser accessibility pass (G); admin screens (G).

Review (2026-10-01): adversarial self-review authorized by the project owner (`engineering-principles.md` §8); no human has reviewed this checkpoint yet, and that review is still owed. Defects found and fixed, each with a regression test:

- Revision 0005 refused two-part permission keys (`user.invite`), so those uses could not be audited (fixed by 0006).
- Locked re-reads returned stale ORM objects: two concurrent refreshes with one token could both succeed, and concurrent removals of one role assignment could fail with an error. Every locked read now refreshes the row.
- A refresh racing a sign-out could deadlock (lock order); refresh now locks the session first.
- Sign-out left no security event.
- Concurrent Argon2 hashing was unbounded (memory exhaustion under a sign-in flood).
- The health routes declared no access rule (now explicitly public).
- `looks_like_token` accepted non-ASCII letters.
- React Aria's native validation blocked resubmitting a form after a server-side field error.
- HTTP client libraries logged request URLs, which for the breached-password check carry a password-derived hash prefix.

Automated verification at commit time: backend `uv sync --locked`; pytest 463 passed (including migration gates: single head `0009`, upgrade/downgrade/upgrade, schema drift, the authorization matrix over every role and gated route, data scopes over PostgreSQL, route coverage, secret leakage); Ruff format and lint clean; mypy strict clean; import-linter 6 contracts kept; pip-audit `--strict` no known vulnerabilities. Frontend: pnpm install frozen; 26 tests passed (including axe); ESLint, TypeScript strict and Prettier clean; banned-words check passed; build passed (148 KB gzipped initial JavaScript); `pnpm audit` no known vulnerabilities; generated API types match the contract. gitleaks v8.30.1 and Semgrep 1.178.0 (CI rule sets) no findings; OpenAPI regenerated and committed; Compose configuration valid.

Open decisions recorded in roadmap §6: pre-joining account access, whether `user.disable` may target administrators, rehire. Known limitation: the progressive sign-in delay applies only to existing accounts (`security-architecture.md` §3.4).

### Checkpoint D — email outbox, invites, password reset, trusted devices, settings (done, 2026-10-01)

Delivered:

- Revision 0010: `notify.email_outbox`, `identity.trusted_devices`, `app.settings`; `password_reset` added to the one-time token purposes and the session revocation reasons. Expand only; the downgrade reverses it for development databases.
- `modules/notify`: the email outbox (`enqueue` in the business transaction, idempotency key per event, template data checked for secrets and contact details) and the worker's dispatcher (`notify.dispatch_email`, every minute: `SKIP LOCKED` claim with a lease, retries with backoff, `failed` after the configured attempts, `cancelled` when an email no longer applies). `platform/email.py`: SMTP sender (STARTTLS, TLS, or plain only in `local`/`test`), whose errors carry only the exception class.
- `modules/identity`: admin invites (`POST /users`, resend, revoke), password reset (request and complete), security emails (new device, account locked once per lock, recovery code used, password changed or reset, MFA changes), trusted devices (`/me/devices`), the keyed attempted-email hash for unknown accounts, lockout and session timeouts read from settings.
- `platform/settings.py` and `modules/settings`: the typed settings registry (defaults in code, overrides in `app.settings`, bounds, rules between settings, secret-shaped names refused) and `GET /settings`, `PUT /settings/{key}`.
- `platform/security/emails.py`: the one canonical email normalization and the HMAC-SHA256 email hash with its own key (`EMAIL_LOOKUP_HMAC_KEY`).
- Frontend: password reset request and completion, recognized browsers on the account security page, the settings page (read-only without the change permission, step-up on save), "Forgot your password?" on sign-in.
- Local Mailpit service in Compose; the integration tests send real SMTP to a Mailpit container.

Decisions (documents updated in the same change):

- Outbox rows name the recipient account, never the address; the worker reads the address at send time and creates invite and reset link tokens then, so a usable token exists only in the email (`database-design.md` §4.9).
- Linking an invited account to an employee needs `employee.lifecycle.manage` over that employee and the employee's work email; `user.invite` alone cannot route an employee's self-service access to another mailbox (`security-architecture.md` §3.1, `authorization-model.md`).
- An unscoped permission decided for a subject employee reaches every employee, narrowed by the assignment's constraints, like `all` (`authorization-model.md`).
- Trusted devices only suppress the new-device email; they never skip MFA or step-up. Default lifetime 90 days, chosen here because the documents left it open; it is a setting (`security-architecture.md` §3.4).
- Settings are not seeded: defaults live in the registry, so removing an override restores the default. `security.*` settings need `security.settings.manage` as well as the route's `settings.manage`.
- The access-token lifetime, step-up window, sign-in challenge and recovery-code count stay fixed in code: they are security properties, not tuning.

Deferred, with where they go: MFA reset and re-enrolment emails, grant requests, elevation and break-glass emails, `super_admin` assignment (E); retention purge of sent outbox rows, expired tokens and revoked devices (F/M5); `app.idempotency_keys` (first idempotent endpoint); the admin user screens, including invite, resend and revoke (G; the API is complete); Playwright end-to-end tests including the email flows through Mailpit (G); a transactional email provider for staging and production (roadmap §6).

Review (2026-10-01): adversarial self-review authorized by the project owner (`engineering-principles.md` §8); no human has reviewed this checkpoint yet, and that review is still owed. Defects found and fixed, each with a regression test:

- A row whose rendering failed stopped the dispatcher's batch and was retried for ever; it now counts as a failed attempt. A row whose last allowed attempt was abandoned mid-send is marked failed instead of sent again.
- Invite activation hashed the new password (Argon2, 64 MiB) before checking the link (a C defect); an invalid link is now refused first, as in the reset flow.
- The browser description in security emails is attacker-chosen; it is now reduced to plain characters so it cannot read as a link or an address.
- Outbox template data accepted contact-detail keys (`email`, `phone`, `address`); they are now refused.
- An unscoped permission decided for a subject employee was always denied, so HR could not link an invite to an employee.
- Planted violations (sessions surviving a password reset, a reset token written to the log, the employee check removed from invites) each made the security tests fail; they were removed.

Automated verification at commit time: backend `uv sync --locked`; pytest 559 passed (including migration gates: single head `0010`, upgrade/downgrade/upgrade, schema drift; the authorization matrix and route coverage with the new routes; secret leakage over the email flows; SMTP delivery to a Mailpit container); Ruff format and lint clean; mypy strict clean; import-linter 8 contracts kept; pip-audit `--strict` no known vulnerabilities. Frontend: pnpm install frozen; 35 tests passed (including axe); ESLint, TypeScript strict and Prettier clean; banned-words check passed; build passed (155 KB gzipped initial JavaScript); `pnpm audit` no known vulnerabilities; generated API types match the contract. gitleaks v8.30.1 and Semgrep 1.178.0 (CI rule sets) no findings; OpenAPI regenerated and committed; Compose configuration valid.

One local full run had a sign-in answer `503` (the rate-limit store missed its 0.5-second timeout, so the limiter failed closed as designed) and a later run stalled near the trusted-device tests; neither repeated in two more full runs and three repeats of the email and sign-in tests. The test helper that drains the outbox is now bounded, so a stall there fails the test instead of hanging the suite.

Remaining risks: email delivery is at least once (a worker stopping between the server's acceptance and the status update resends after the lease); the dispatcher sends one email at a time, which suits current volumes; staging needs a transactional email provider (roadmap §6) before invites and resets work outside development.

#### Checkpoint D final verification (2026-10-05)

Scope: the whole M1 implementation as committed through Checkpoint D, plus the two follow-up commits `8100765` (fixes found while writing the local development guide) and `45fdf26` (`docs/local-development.md`, `run-backend.cmd`, README and CLAUDE.md pointers, `local-logins.md` ignored), before pushing them to `main`. No Checkpoint E work.

Review: adversarial self-review authorized by the project owner (`engineering-principles.md` §8). No human has reviewed this code; the second-engineer review of A–D remains owed. The review re-read the identity, access, audit, notify and settings modules, the authorization engine and route declarations, migrations 0001–0010 (grants, `SECURITY DEFINER` functions, triggers, dynamic SQL), the rate limiter, CSRF and client-address handling, and the local development guide, as an attacker looking for privilege escalation, IDOR, MFA, invite and reset bypasses, session and refresh-token abuse, audit tampering or leakage, and secret leakage.

Fixes from `8100765`, recorded here because the plan did not yet list them:

- `python -m app.cli bootstrap-super-admins` failed on a fresh database: the CLI process did not import every model, so foreign keys into other modules could not be resolved. It now imports `app.metadata`; `tests/unit/test_cli_metadata.py` checks it in a separate interpreter, as the CLI runs.
- The hourly `audit.ensure_partitions` job always ended in error after its work committed: its log call used the reserved `LogRecord` key `created`. The key is now `partitions_created`; the worker test captures the log at INFO and fails without the fix.

Defects found and fixed in this verification, each with a regression test that failed before the fix:

- `audit.ensure_partitions` accepted a month's partition that was attached for another month's range, and ignored a default partition. Either left a month without its own partition while the job reported success, and a default partition would take rows that must instead fail the audited change. Revision 0011 makes the function fail loudly in both cases (`test_ensure_partitions_fails_for_a_partition_attached_with_the_wrong_range`, `test_ensure_partitions_fails_when_a_default_partition_exists`).
- `POST /me/password` counted wrong current passwords towards the lockout but kept checking the current password while the account was locked, so a session (after step-up) could keep guessing the password past the lockout. A locked account's password is no longer checked there; the answer is `422` `account.locked` on `current_password` (`test_a_locked_account_cannot_check_its_password_through_a_password_change`).

Checked and found sound: sign-in answers and timing for unknown, locked, disabled and invited accounts; single-use MFA challenge with an attempt limit; TOTP replay protection; recovery codes giving only an enrolment session; refresh rotation with reuse detection and lock order; sign-out and revocation; invite activation (link checked before hashing, enrolment token bound to the browser, invite retired on activation); password reset (one answer for every email, link checked before hashing and again under lock, every session and device revoked); new-device detection (cookie digest only, never skips MFA); route declarations and allow-lists; scope and SoD checks (SOD-3, SOD-4, SOD-8, SOD-10, the two-super-admin minimum); audit append-only triggers, database-assigned IDs and `recorded_at`, personal and contact field refusal, NUL refusal; `SECURITY DEFINER` functions with fixed `search_path` and `format('%I')` identifiers only; no string-built SQL in the application; rate-limit keys HMAC-ed, fail-closed for authentication; `X-Forwarded-For` trusted only from configured proxies; no secrets or personal data in logs, audit details or the outbox; the local development guide, re-run end to end on a fresh Compose project.

Automated verification: backend `uv sync --locked`; pytest 565 passed (including migration gates: single head `0011`, upgrade/downgrade/upgrade, schema drift); Ruff format and lint clean; mypy strict clean; import-linter 8 contracts kept; pip-audit `--strict` no known vulnerabilities; OpenAPI regenerated unchanged. Frontend: pnpm install frozen; 35 tests passed; ESLint, TypeScript strict and Prettier clean; banned-words check passed; build passed; `pnpm audit` no known vulnerabilities; generated API types match the contract. gitleaks v8.30.1 and Semgrep 1.178.0 (CI rule sets) no findings; `docker compose config` valid.

Open decision, not made here: the account lockout is documented for sign-in (§3.4). Step-up failures count towards it, but step-up does not refuse a locked account; it is bounded by its own limit of 5 attempts per 5 minutes per session. Whether a lock should also block step-up (which would let anyone who knows an email address block a signed-in user's step-up by locking the account) is for the project owner to decide.

Remaining risks: the human second-engineer review of A–D is still owed; the risks listed above for D (at-least-once email delivery, one email at a time, no staging email provider yet) are unchanged; Playwright end-to-end and real-browser accessibility tests arrive with G.

CI on the push of `ac3f647` (2026-10-05): backend tests 2 failed, 563 passed; every other job passed. Neither failure reproduced locally or in a Linux container. Root causes and fixes (`fix(notify)` commit after `ac3f647`):

- `test_failures_retry_with_backoff_then_stop_as_failed`: all API tests share one database, so the outbox held due rows left by earlier tests. The dispatcher claims at most 20 rows per run, ordered by `next_attempt_at` only, and rows retried in the same run share the same `next_attempt_at`, so which tied rows the database returned decided whether the test's own row was claimed. Reproduced with 25 leftover due rows (attempts `[0, 1, 2, 3, 3]` instead of `[1, 2, 3, 4, 5]`). The claim now orders ties by `id` (oldest first), with a regression test that fails without it; the tests that assert on one dispatcher run (retry and backoff, concurrent dispatchers, render failure) run on a database of their own (`isolated_api`). The retry test also checks that a row is not retried one second before its backoff ends. Backoff, batch size and attempt limit are unchanged.
- `test_partition_maintenance_task_recreates_missing_partitions` (assertion added in `8100765`): starting the worker also defers the hourly run for the current slot, which, depending on timing, runs before the worker stops (creating nothing) or stays queued. Both are correct; the test expected exactly one run. It now asserts that every attempted run succeeded, each logged its outcome, and together they created exactly the two missing partitions. It still fails with the original logging defect.
- Found while repeating the tests: the Mailpit fixture waited for the web UI only, and the first SMTP connection could be dropped (1 in 15 local runs). It now waits for an SMTP greeting.

Verification: outbox and worker tests 10/10 runs on Windows and 5/5 in a Linux container after the fix; full gate: pytest 566 passed, every other CI step clean.

### Checkpoint E — admin flows: MFA reset, grant requests, administrator accounts (done, 2026-10-09)

Delivered:

- Revision 0012 (expand only): `identity.users.mfa_reenrolment_required`, with the MFA invariant widened (an active account keeps a confirmed factor unless it is waiting to re-enrol) and a deferred trigger refusing a `full` session for an account without a confirmed factor or waiting to re-enrol; the `mfa_reenrolment` token purpose, the `mfa_reset` revocation reason and the outbox templates `identity.mfa_reset` and `access.grant_request`; `access.role_grant_requests` with CHECK constraints for the limits and a history trigger (no delete, forward transitions only, each decision recorded once); `access.user_roles.grant_request_id` with an insert trigger that matches every self-granted or `super_admin` assignment to its request.
- MFA reset (`POST /users/{id}/mfa-reset`, `security.mfa.reset`, step-up, reason, never one's own account): revokes every factor, unused recovery code, session, trusted device and pending sign-in challenge, and emails a re-enrolment link (24 h by default, setting `security.mfa_reenrolment.ttl_hours`). Re-enrolment (`POST /auth/mfa-reenrolment/{token}`, `/mfa-reenrolment/:token`) needs the link and the password, opens an enrolment-only session, and confirming the new authenticator returns new recovery codes.
- Grant requests (`/role-grant-requests`): elevation (at most 8 hours, approved by another super admin, starting at approval), super admin assignment (`POST /users/{id}/roles` with `super_admin` answers `202` with a request; SOD-8 and SOD-10 on approval), break-glass (`system_admin` only, at most 1 hour, active at once, acknowledged afterwards by another super admin), reject, withdraw and end early. Every action needs step-up and an account holding `super_admin`. Access stops at `ends_at` without a job; `access.expire_grants` (worker, every minute) records expiries. Notification emails to the other super admins (and auditors for break-glass). `GET /me/elevation` feeds the elevation banner.
- Administrator accounts (decided by the project owner): an account holding `system_admin` or `super_admin` is disabled or enabled only by a `system_admin` holder (`403 user.admin_account`); disabling a super admin, or removing the role, must leave at least two active super admins (`409 user.min_super_admins`, `409 role.min_super_admins`), with the holders locked before the account so concurrent disables cannot race. Removing someone's `super_admin` role ends their temporary access and withdraws their pending requests.
- Lockout and step-up (decided by the project owner): the lockout guards every password check (sign-in, password change, re-enrolment) but does not block step-up, which keeps its own limit of 5 attempts per 5 minutes per session; step-up failures still count towards the lockout.
- Audit records of an elevated or break-glass session carry the grant request (`Actor.grant_request_id`).
- Frontend: Access requests page (`/admin/access-requests`), MFA re-enrolment page, elevation banner in the app shell, `Select` design-system component, recovery codes after re-enrolment on the enrolment page.

Deferred, with where they go: the admin user screens (list, invite, disable, MFA reset, sessions, roles) (G; the API is complete); Playwright end-to-end tests of re-enrolment and elevation (G); the audit sealer and tamper suite (F).

Review (2026-10-09): adversarial self-review authorized by the project owner (`engineering-principles.md` §8). This is not an independent review: no second engineer has reviewed Checkpoint E, and the human second-engineer review of A–E is still owed. The self-review re-read the administrator-account rules, the MFA reset and re-enrolment flow, the grant-request approval, revocation and break-glass paths, revision 0012, and the step-up flags in the catalog, and the owner ran a manual sign-in test (password, authenticator code, step-up, sign-out and sign-in again) against a local acceptance database.

Defect found and fixed, with a regression test that failed before the fix:

- Every elevation request and break-glass answered `500` in the running API: the API and worker processes never imported the `org` models, so the foreign keys from `access.role_grant_requests` and `access.user_roles` to `org.departments` and `org.locations` could not be resolved when a row was flushed (the same defect `8100765` fixed for the CLI). The test suite imports every model, so it did not show. `app/main.py` and `app/worker.py` now import `app.metadata`, and `tests/unit/test_cli_metadata.py` checks the CLI, the API and the worker each in a separate interpreter.

One accepted risk: the administrator-account check reads the target's roles before locking, so a `super_admin` approval committing in that instant could let a `user.disable` holder disable a brand-new super admin once; the two-super-admin minimum is unaffected because approvals only add super admins.

Automated verification: backend `uv sync --locked`; pytest 594 passed (migration gates with single head `0012`; local `hrms` and `hrms_acceptance` both at `0012`); Ruff format and lint clean; mypy strict clean; import-linter 8 contracts kept; pip-audit `--strict` no known vulnerabilities; OpenAPI regenerated and committed. Frontend: pnpm install frozen; 48 tests passed (including axe); ESLint, TypeScript strict and Prettier clean; banned-words check passed; build passed; generated API types match the contract; `pnpm audit` clean after updating the transitive `source-map-js` from 1.2.1 to 1.2.2 (GHSA-68fv-2mgg-jv7q, build tooling only). `docker compose config` valid. Gitleaks v8.30.1 and Semgrep 1.178.0, the CI images and rule sets, run locally through Docker: Semgrep 0 findings on the tracked files and 0 on the 40 new or changed source files scanned explicitly; Gitleaks no leaks in the staged changes and, after the commit, in the full history. CI runs both again on the push to `main`.

### Checkpoint G — application UI, with the first M2–M4 slices (delivered 2026-10-10, not committed)

Scope set by the project owner (2026-10-10): the complete visible application in one pass, implementing missing MVP endpoints through the existing architecture where no Sigvitas policy value is needed.

Delivered:

- Revisions 0014 (org `version` columns; `people.employee_personal`; append-only `people.employee_status_history`; directory-search indexes; `org.read` and `holiday.read` for `hr` and `hr_admin`), 0015 (`attendance` schema, append-only `attendance_events`), 0016 (`leave` schema with holiday calendars, holidays and location assignments). All expand only.
- Backend modules: `org` (locations, departments, designations with optimistic concurrency and audit), `people` (directory, scoped employee list and statistics, create, edit, job history, status changes, personal tier, `/me/profile`, `/me/jobs`, `/me/personal`), `attendance` (clock events with server time, sequence checks under a per-employee lock, idempotency keys, rate limit; today, history, team today view), `leave` (holiday calendars, `/me/holidays`). `GET /users/{id}` and `q`/`employee_id` filters on `GET /users`. Platform: `concurrency.py` (ETag / If-Match), text-key cursors, `records.change_of`.
- Frontend: design system (Badge, Panel, Stat, DataTable with stacked rows on phones, EmptyState, retryable ProblemMessage, Toast, ConfirmDialog, Tabs, ComboBox, SearchField, TextArea, Checkbox, DescriptionList, Avatar, LoadMore); app shell (collapsible sidebar, drawer below 1024 px, bottom bar on phones, page titles, directory search, account menu); the dashboard as home; Employees, employee record, Add employee, Directory, Organization, My profile, My attendance, Team attendance, Holidays, Holiday calendars, Users, user detail, Roles, Security events, Audit log. Every page is lazy-loaded, sign-in included (initial JavaScript about 185 KB gzipped).

Not built, with reason: leave types, policies, ledger and requests; shifts, attendance policies, day status and corrections (Sigvitas policy values, roadmap §6); identifiers and bank accounts (identifier types not chosen); documents, in-app notifications, reports and exports (M5). None is in the navigation.

Review: adversarial self-review of scoping, hidden existence, audit of personal reads and redaction, self-service limits, attendance locking and idempotency, and the 0014 grants. No human review yet; it is owed with A–F.

Automated verification (2026-10-10): backend Ruff format and lint clean, mypy strict clean (105 files), import-linter 10 contracts kept, pip-audit `--strict` no known vulnerabilities; targeted pytest runs passed: new people, attendance, holiday and user-lookup integration tests, attendance timeline unit tests, the authorization matrix over every role with the new routes, route coverage, schema drift, migration cycle and database roles. The full backend suite was started but stopped by the host for low memory after about 140 tests (all passing to that point); it has to be run again. Frontend: 79 tests passed (including axe), ESLint, TypeScript strict, Prettier and the banned-words check clean, build passed, `pnpm audit` no known vulnerabilities, generated API types match the contract. `docker compose config` valid. Not run: Gitleaks and Semgrep (deferred because of the memory pressure). The local `hrms` database is still at `0013`; `alembic upgrade head` (run by `run-backend.cmd`) applies 0014–0016.


### M2 organization and employee management (delivered 2026-10-11, not committed)

Scope set by the project owner (2026-10-11): complete organization setup and employee management on top of Checkpoint G, reusing what exists. Most of the requested workflow already existed (employee create, edit, job history, status changes, personal tier, self-service, directory, invite-based account linking); this change fills the gaps.

Delivered:

- Revision 0017 (expand only): `org.departments.description` (optional, 1 to 1000 characters) and `ix_employee_jobs_designation_id`.
- Organization rules: archiving a department, designation or location is refused (`409 org.in_use`) while any employee who has not left has a current or future job row in it, and a department with active departments inside it (`409 department.has_active_children`); a parent department must be active (`422 department.parent_archived`). Placing an employee locks the chosen units `FOR SHARE`, so a concurrent archive and placement cannot both succeed.
- Employee list: `designation_id` filter and `sort` (`name`, `code`, `joined_newest`, `joined_oldest`), keyset-paged in every order.
- `GET /employees/statistics`: `recent_joiners` and `by_department`, `by_designation`, `by_location` headcounts, all within the caller's `employee.profile.read` scope.
- Frontend: Organization opens on an Overview tab (figures, set-up progress with an action per missing step, headcounts per unit linking to the filtered employee list); tabs are in the URL (`/organization?tab=locations`); each list has search, a show-archived switch and an employees column; edit dialogs warn before archiving and the confirming button says "Archive …"; department descriptions. Employees: designation filter and sort order, kept in the URL. Dashboard: set-up steps link to the tab, headcounts by department and location, recent joiners. Users list says "Account only" for an account with no employee record.
- `run-backend.ps1` and `scripts/check_local_target.py`: the startup script checks the target database, migration revision and anchor bucket before migrating and stops on a mismatch (docs/local-development.md, Windows shortcut).

Target database: `hrms_local_test` only, at `0017` (applied by the checked startup script on 2026-10-11, anchor bucket `hrms-audit-anchors-local-test`). `hrms` stays at `0016`, `hrms_acceptance` at `0012`; neither was migrated. No organization or employee rows were created in any local database.

Verification (2026-10-11):

- Backend: full suite 685 passed (7 min 15 s) before the `FOR SHARE` change; afterwards the people, attendance, holiday, invitation, authorization matrix and scope, security and contract tests, 64 passed. One test (`test_email_flow_secrets_stay_in_the_email`) failed once in that subset because it counted emails queued by earlier tests in the shared outbox; it now counts only its own recipients and passes in that order. Ruff format and lint clean, mypy strict clean, import-linter 10 contracts kept, pip-audit `--strict` no known vulnerabilities. Gitleaks (v8.30.1): no leaks in the history or the changed files. Semgrep 1.178.0 (`p/python`, `p/security-audit`): 0 findings over 257 git-tracked files; the three new untracked files (revision 0017, `scripts/check_local_target.py`, `Headcounts.tsx`) were not in that scan.
- Frontend: 84 tests passed (7 files, including axe on the organization overview), ESLint, TypeScript strict, Prettier and the banned-words check clean, build passed, `pnpm audit` no known vulnerabilities, API types regenerated from the contract.
- Integration: the API on `hrms_local_test` answers `/api/health/ready` with `ok` and `401` without a session. No signed-in browser test was run: signing in needs the account owner's authenticator.

Security and authorization review (adversarial self-review; no human review yet, owed with A–F): no new endpoint and no permission change. New statistics are computed under the existing `employee.profile.read` scope filter and hold only unit names and counts; a manager sees counts for their team only (tested). The archive refusal tells an `org.manage` holder how many people are placed in a unit across the whole organization, a count without identities; this is accepted for an HR administrator. Sort and filter values are validated (`422` on anything else, tested). Organization changes keep auditing old and new values, including the description. Self-service limits and account linking (work email, `employee.lifecycle.manage`, one account per employee) are unchanged and covered by existing tests.

Remaining and decisions still needed: identifier types, identifiers and bank accounts; emergency contacts; rehire; linking an already existing account to an employee record (today only at invite); a department head picker on the department form (the API accepts `head_employee_id`); the Sigvitas inputs in roadmap §6.

### Account linking, department heads, employment definitions and leave management (delivered 2026-10-11, not committed)

Scope set by the project owner (2026-10-11): finish the organization and employee workflows and build an initial, fully configurable leave module, with no Sigvitas policy value assumed.

Delivered:

- Account linking: `POST /users/{id}/employee-link` (`user.invite` plus `employee.lifecycle.manage` over the employee, step-up, a reason, the work-email rule; never one's own, an administrator or a disabled account; one account per employee under a row lock and the unique constraint; idempotent; audited without personal values). On the employee record, **HRMS account** offers **Link an existing account** and states when no account uses the work email or the account belongs to another record.
- Department heads: `head_employee_id` on create and update, validated server-side (exists, has not left); `head_name` in responses; a head picker on the department form for people who can read employee records.
- Employment definitions (product-requirements EMP-7): `employment=current|future|former` on `GET /employees`, `future_joiners` and `former` in the statistics; the employee list defaults to employed today, the dashboard's figures link to the matching list, and both say what "employed today" means.
- Leave (revision 0018, see database-design §4.6): leave types with every rule chosen by HR; effective-dated policies per scope; the append-only ledger; upfront allocation per leave year (at most once per employee, type and year); adjustments and opening balances with reasons; requests with preview, half days, holidays left out when the type says so, overlap refusal (EXCLUDE), balance checks under a per-employee lock, idempotency keys and a rate limit; cancellation and cancellation decisions; decisions with SoD, a note to reject, idempotency keys and a row lock against concurrent approvers; `leave.request` emails without reasons through the outbox; the calendar and lists without reasons. Pages: My leave, Request leave, the request page, Leave approvals, Leave calendar, Leave overview, Leave balances, Leave types, the Leave tab on the employee record, and dashboard panels.
- Platform and tests: a contract test refuses two models sharing a schema name (found and fixed `DayView` and `CalendarView` clashing with attendance and holidays); the frontend test setup empties React Aria's live announcer between tests.

Database: only `hrms_local_test` holds `0018`. It was applied at 15:04 IST when the local-test startup script was restarted (its target check passed); a restore point was taken afterwards (`hrms_local_test-0018-20261011-151748.dump`, custom format, in the session scratchpad, not in the repository). `hrms` stays at `0016`, `hrms_acceptance` at `0012`, `hrms_m1verify` at `0013`. This work created or changed no leave configuration, employee or account data in any local database.

Verification (2026-10-11): backend full suite **721 passed** (6 min 22 s), including the new account-link (4), leave API (12), leave rules (15), people (18) tests, the authorization matrix over every role with all new routes, route coverage, schema drift, migration upgrade and downgrade, secret leakage and the contract tests; Ruff format and lint clean, mypy strict clean (194 files), import-linter 10 contracts kept, pip-audit `--strict` no known vulnerabilities. Frontend **98 passed** (8 files, including axe on My leave and the organization overview), ESLint, TypeScript strict, Prettier and the banned-words check clean, build passed with 185.1 KB gzipped initial JavaScript (budget 200 KB), `pnpm audit` no known vulnerabilities, API types regenerated. Gitleaks v8.30.1: no leaks in the history or in the 78 changed and new files. Semgrep 1.178.0 (`p/python`, `p/security-audit`): 0 findings over 276 files, new files included (scanned outside git). The running API on `hrms_local_test` serves the new routes (401 without a session). No signed-in browser test was run: no browser automation is available here and signing in needs the account owner's authenticator.

Security and authorization review (adversarial self-review; no human review yet, owed with A–F): every new route declares a permission and is in the authorization matrix; leave reads use the scope filter, a request outside scope is not found; reasons and notes reach only the employee and their deciders, and a reader other than the employee is audited; SOD-1 and SOD-6 are enforced and tested; super admins, system admins and auditors hold no leave permission (tested); concurrent approvals leave one decision and concurrent submissions cannot overdraw a balance (tested); the ledger and decisions are append-only and policies versioned by trigger; emails carry no reason; the audit backstop treats `amount` as money, so day counts are recorded as `days` and `accrual_days`.

Remaining and decisions still needed from Sigvitas: the leave types and every rule (paid, approval, half days, holidays and weekly offs counting); leave year start; allocation amounts and whether they are upfront or periodic; proration on joining and exit; carry-forward caps, expiry and lapse; negative balance limits; attachment rules; who approves when there is no reporting manager; whether HR may decide team leave by default; opening balances at go-live. Not built: periodic accrual and year-end jobs, opening-balance import, attachments, weekly offs (need shifts), multi-level approval, leave reports and exports.

### Role-based dashboards (2026-10-11, not committed)

Diagnosis: the HR widgets were not missing; the signed-in account could not see them. In `hrms_local_test` the only active account, `hrms.admin@example.com`, holds `super_admin` alone, which by design reaches no employee, attendance or leave data (CLAUDE.md rule 16); it has no employee record and had never requested an elevation. The second super admin, `hrms.admin2@example.com`, is still invited, so no elevation could be approved. The seeded role permissions match the code (`hr_admin` holds every dashboard permission; `super_admin` only `role.read` among them).

Already built and verified: the people figures, headcounts, recent joiners, organization set-up, attendance today, leave waiting, my leave, access requests and sign-ins widgets, each behind its permission. Missing or weak, now changed: a former-employees figure (EMP-7) beside employed today and joining in the future, in a five-column figure grid (two on phones, three on tablets); the organization set-up check showed nothing when its lists failed, and now says so with a retry; the people empty state offers Add an employee; shortcuts to Employees, Leave approvals, Leave overview, Leave balances and Leave types; a panel telling a super admin how to get time-bound HR access; the no-record banner names leave. No backend or permission change was needed.

Tests: `tests/integration/test_dashboard_access.py` (8) asks every dashboard data source as `super_admin`, `hr_admin`, `hr`, a manager and an employee and asserts who gets data and who is refused, that a manager's and an employee's figures cover only their team or themselves, and that a super admin reaches the HR figures only through an elevation approved by another super admin (step-up, SOD-9, audited) that stops applying at its end. With the authorization matrix, scopes, grant requests and route coverage: 47 passed. Frontend: six role-based dashboard tests (super admin, HR admin, HR, manager, employee, failed figures with retries); 104 passed; ESLint, TypeScript strict, Prettier, the copy check and the build clean. No signed-in browser test was run (no browser automation here; signing in needs the account owner's authenticator).


### Remaining HRMS work in one pass: M3 to M5 slices, storage, scanning, roles reference (2026-10-15, not committed)

Scope set by the project owner (2026-10-15): finish as much of the remaining HRMS as can be done safely in one pass, verify it, document the actual system and prepare for browser acceptance; no Sigvitas policy value assumed, no recruitment features.

Delivered in the first session of the day (it stopped at its usage limit before verifying everything; this entry records it):

- Revision 0019: in-app notifications and per-person preferences; the header bell with the unread count; leave and correction emails follow the email preference; no reason or note in any notification.
- Revision 0020: shifts and versions with weekly offs, location default shifts, assignments and attendance policies; day status derived when read by the pure `derive_day` (no stored projection); corrections requested by employees and decided by managers or HR (SOD-1), HR-recorded corrections (SOD-7), voids instead of deletes; locks; the set-up gap view, the daily register and the period report.
- Revision 0021: periodic accrual in the worker with proration; the year end with a preview (carry-forward cap, lapse or keep, expiry of carried days); the all-or-nothing opening-balance import; weekly offs from shifts counted in requests; the leave report.
- Revision 0022: documents with categories by sensitivity, content-checked uploads, versions, ClamAV scanning in the worker, audited 60-second download links.
- Revision 0023: six export types with step-up, scope fixed at request time, formula-injection protection, requester-only downloads for 24 hours.
- Revision 0024: emergency contacts; the one-step exit (record exited, account disabled and sessions revoked in one transaction).
- Revision 0025: `shift.read` for `hr` and `hr_admin`.
- Frontend: Attendance set-up, corrections, Documents, Document categories, Reports, Notifications, emergency contacts, the exit dialog, dashboard panels.

Verified and changed in the second session:

- **Database.** `hrms_local_test` was found at `0025`, not `0018`: the startup script had applied 0019 to 0025 at 11:02 IST when it was restarted, with no backup taken first. Comparing its schema with a fresh migration showed one difference: `ck_export_jobs_storage_key` came from an early draft of 0023 (an f-string turned `{36}` into `36`), so no export could ever finish there. Revision **0026** puts the intended check in place on every database (a no-op where 0023 was right). Backups were taken and verified by restoring each into a throwaway database (`hrms_local_test-0025-20261015-170254.dump` and `hrms_local_test-pre-0026-20261015-173931.dump`, custom format, in the session scratchpad); 0026 was then applied after the target check. `hrms_local_test` is at `0026`. `hrms` (0016), `hrms_acceptance` (0012) and `hrms_m1verify` (0013) were not migrated; their revisions and table statistics are unchanged, and `hrms_acceptance` and `hrms_m1verify` match a fresh migration to their revisions exactly. `hrms` differs from a fresh 0016 only in the audit insert trigger (`enforce_recorded_at` instead of `assign_record_keys`): it was migrated with an uncommitted draft of revision 0005 on 2026-10-01. That predates this work; fixing it needs a repair revision applied to `hrms`, which is the owner's decision.
- **Lint and types.** Ruff (long SQL lines in 0020, 0022, 0023, rewrapped without changing any statement; the two lines inside PL/pgSQL bodies carry `noqa` because PostgreSQL keeps function bodies as written) and one mypy error in a test were fixed. A fresh migration produces the same schema before and after the rewrap.
- **Security fixes.** Export requests counted against the general API rate limit, which fails open when Redis is down; they now use the export scope (fail closed) at the documented 5 per hour, with one export in preparation per person at a time (`409 export.in_progress`, serialized by an advisory lock). Document downloads and export downloads had no rate limit; both are now limited to 60 per user per hour and fail closed (api-architecture §9).
- **Tests added or fixed.** A scanner that cannot be reached leaves a version pending and, after five attempts, `failed`, never downloadable; the signed link carries `X-Amz-Expires=60`; a second export while one is queued is refused; the limits use fail-closed scopes. `test_authorization_scopes` expected the account baseline before the notification permissions joined it. The test helper that delivers the outbox now allows for emails earlier tests left queued (the full suite had failed on it after the new tests), and says which templates were left if it still fails. Frontend: navigation for each role from the real grants (`src/app/navigation.test.ts`).
- **Roles.** `docs/roles-and-permissions.md`: the six business roles mapped to the eight role keys (all six exist; nothing added or renamed), each role's duties, pages, limits and forbidden actions, the sensitive-data tiers, step-up and audit, elevation and break-glass, and the 14 account and employee workflows. Its matrix and `frontend/src/test/role-permissions.json` are generated by `scripts/role_matrix.py` and checked by `tests/contract/test_role_matrix_doc.py`, which delivers authorization-model §9's promise; the hand-written matrix in authorization-model §4.1, which had drifted, now points there.
- **Storage and scanning.** `scripts/create_local_files_bucket.py`; `run-backend.ps1` gains `-FilesBucket` (each database its own bucket, checked by `scripts/check_local_target.py --files-bucket`) and starts ClamAV when `CLAMAV_HOST` is set. The local `.env` gained `FILES_*` and `CLAMAV_*` (backed up first); `hrms-files-local-test` exists and refuses anonymous requests (403). Live ClamAV 1.4.3: a clean PDF is clean, the EICAR test string is `Eicar-Test-Signature`, an unreachable scanner is an error; an upload through the API to RustFS, scanned by the real ClamAV, became downloadable through a 60-second link. EICAR cannot travel the upload path, because the content allow-list refuses it first.
- **Local credentials.** `local-test-credentials.md` held plaintext passwords; they were moved to `.local-secrets/` (git-ignored, readable only by the owner's Windows account) and the guide now holds no secret. Both passwords were seen in AI session transcripts and should be changed.

Verification (2026-10-15): backend full suite **788 passed** (13 min 24 s; the run before the fixes was 782 passed and 2 failed, both explained above); Ruff format and lint clean, mypy strict clean (248 files), import-linter 12 contracts kept, pip-audit `--strict` no known vulnerabilities. Frontend **118 passed** (10 files, including axe and the new navigation tests), ESLint, TypeScript strict, Prettier and the banned-words check clean, build passed, `pnpm audit` no known vulnerabilities; the OpenAPI contract is unchanged. Gitleaks v8.30.1: no leaks in the 13 commits or in the 413 tracked and untracked files. Semgrep 1.178.0 (`p/python`, `p/security-audit`): 0 findings over 337 files, untracked included (one finding in the new bucket script, `urllib` with a URL from settings, was fixed by using an unsigned boto3 client). Nothing is staged or committed.

Not done: no signed-in browser test (signing in needs the owner's authenticator, and no browser automation is available here); no account exists in `hrms_local_test` for `hr_admin`, `hr`, a standing `system_admin`, a manager or an employee (the supported way to create them needs the owner's authenticator; the steps are in `local-test-credentials.md`); EXIF stripping of uploaded images; document retention; the attendance policy preview; overtime approval; leave attachments and multi-level approval; identifiers and bank accounts; payroll and compensation (nothing in the catalog yet).

Review: adversarial self-review of the new routes, authorization in services, document and export boundaries, rate limits, migrations and secrets; no human review yet, owed with A–F.

### Organization set-up fix and module acceptance (2026-10-16, not committed)

Reported: on `hrms_local_test` the Organization page showed **Add department** but organization set-up did not work, so no employee could be added.

Diagnosis: the API, permissions (`org.manage`, held by the account through an approved `hr_admin` elevation), audit and database were correct and fully tested. The defect was in the page: each list rendered its add/edit dialog inside the list body, and the list shell replaces the body with the empty state when there are no records, so with an empty list (the state of `hrms_local_test`) the button changed state but no dialog was ever mounted. The existing test added a department to a list that already had one. Details in `module-acceptance.md` §4.

Changed: `OrganizationPage.tsx` (the shell renders the dialog through its own `dialog` prop); `People.test.tsx` (each Add dialog opens from an empty list; the first location shows an API field error); `backend/tests/integration/test_onboarding_journey.py` (HR admin creates a location, department and designation through the API, adds an employee, sends the linked invite, the employee activates it with password and TOTP and reaches only their own records; audit rows checked); `module-acceptance.md` (definition of complete, status by module, browser scripts, attendance location and payroll status with phased plans); CLAUDE.md document table. No migration, no permission, no API change. No local database was written to: `hrms_local_test` was only read (revision `0026`, no organization or employee rows).

Not done: no signed-in browser run (no browser automation here, and signing in needs the owner's authenticator); attendance location controls and payroll are Phase 2 and need the owner's approval and Sigvitas inputs (`module-acceptance.md` §6 and §8).

### Dialogs, declared working days, leave policy and staff provisioning (2026-10-16, not committed)

Dialogs: tall dialogs (Add shift, Add leave type) were cut off. The shared `Dialog` centred its panel with flexbox inside a scrolling overlay, which pushes the top of a taller-than-window panel out of reach. The overlay now follows `--visual-viewport-height`, the panel is capped, only the body scrolls, and every dialog's buttons (30 rows) use the new pinned `DialogActions`; `ConfirmDialog` shares the layout. Tests: `src/design-system/Dialog.test.tsx`; `e2e/dialogs.e2e.ts` runs seven add dialogs at five screen sizes in Chromium with a fake API (35 passed; the tall dialogs fail with the old layout, checked).

Workweek: shifts already express Monday to Friday with Saturday off (weekly offs per weekday, optionally per week of the month). Revision **0027** (expand only) adds `leave.holidays.kind` so a holiday calendar can declare a working day on a usual day off: attendance judges that date as a working day and leave day counting counts it, at the calendar's locations only; the shift is unchanged; audited with `kind`. No shift time or policy value was entered or assumed.

Leave: no change. Every supported value is configuration and audited; employee records hold no gender and policies select by location and employment type only, so a gender-based entitlement is neither configured nor possible. Decisions and the eligibility-group design are in `module-acceptance.md` §7.2.

Accounts: the HR and HR admin workflows were checked against the code and documented with their verification state (`roles-and-permissions.md` §6). New end-to-end test `test_staff_provisioning_journey.py`. Found while testing: disabling an account needs step-up (already documented, now covered end to end).

Database: no local database was written to. `hrms_local_test` was read only (revision `0026`); 0027 is not applied anywhere. The local-test startup script applies outstanding migrations when it starts, so take and verify a backup before restarting it.

Verification (2026-10-16): backend full suite 790 passed and 2 failed under heavy machine load (a 503 from the fail-closed sign-in rate limiter and a transaction timeout in the concurrent-sealer test; both untouched by this change and both pass when rerun); targeted runs of the holiday, leave, attendance rules, schema drift, migration, authorization matrix and both journey tests passed. Ruff, mypy strict (252 files), import-linter (12 contracts) and pip-audit clean. Frontend 126 passed, Playwright 35 passed, ESLint, TypeScript strict, Prettier, the copy check and the build clean, `pnpm audit` no known vulnerabilities; API contract and types regenerated. No signed-in browser run by engineering.

### Calendar actions, record actions and themes (2026-10-16, not committed)

- Holiday calendars: entries can be edited (`PATCH /holiday-calendars/{id}/holidays/{holiday_id}`, name and optional only); calendars renamed, archived (refused while a location uses them) and restored from the calendar page; an archived calendar is read-only in the API and the page; the list shows working days and has an explicit Open action; removing a working day explains what changes. No migration.
- Record actions reviewed for every list and detail screen (`module-acceptance.md` §9). New backend guard: a leave type is not archived while a request of it waits for a decision. Archiving shifts, leave types and document categories now explains the consequence and uses a danger button, as organization records already did.
- Light, dark and system themes from the account menu, applied before the first paint by `public/theme-init.js`; the dark theme redefines the colour tokens. IBM Plex Mono added for codes. Checkbox borders raised to `neutral-500` for contrast.
- New: `backend/tests/integration/test_record_actions.py`, `frontend/e2e/theme.e2e.ts`, `frontend/e2e/support.ts` (shared fake API), unit tests in `App.test.tsx`, `AttendanceAndAdmin.test.tsx`, `Leave.test.tsx`. `test_declared_working_days.py` no longer assumes its calendar is first in the list.

### Checkboxes, scrolling and data views (2026-10-16, not committed)

- Checkbox defect fixed at the cause (missing `selected`/`pressed` variants), preference saves made immediate with rollback, toasts grouped; tab-strip scrollbar and non-sticky table headers fixed; slim themed scrollbars, stable gutter, scroll padding. See `module-acceptance.md` §11.
- Expandable table details, attendance day timelines and month view, audit record details, department hierarchy, `ViewSwitch`, `animate-reveal`. See `module-acceptance.md` §12.
- New: `e2e/checkboxes.e2e.ts`, `e2e/scrolling.e2e.ts`, `e2e/data-views.e2e.ts`, `src/design-system/Fields.test.tsx`, `src/design-system/DataTable.test.tsx`, `src/design-system/ViewSwitch.tsx`, `src/features/attendance/DayDetails.tsx`, `src/features/attendance/AttendanceMonth.tsx`, `src/features/organization/DepartmentTree.tsx`; backend preference read-back test.

