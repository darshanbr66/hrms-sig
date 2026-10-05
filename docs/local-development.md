# Local development and test sign-in

This is the single guide for running Sigvitas HRMS on a developer machine and signing in from a
browser. Everything here is for local development only. Never use these steps, example addresses
or generated values for staging or production, and never use real Sigvitas credentials.

Commands are shown for Windows PowerShell, the main development platform. Where macOS or Linux
differ, the difference is noted. Run each command from the folder named above it.

## 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop (Docker Engine with Compose v2) | Current | Runs PostgreSQL 18, Redis and the Mailpit mail catcher. The backend tests also start their own containers. |
| [uv](https://docs.astral.sh/uv/) | Current | Installs and manages Python 3.13 for the backend (`backend/.python-version`). You do not need to install Python separately. |
| Node.js | 20.19 or later (`frontend/package.json` `engines`) | |
| pnpm | 12.8.1, through corepack (`frontend/package.json` `packageManager`) | Run `corepack enable` once; corepack then provides the pinned pnpm. |
| Git | Current | |
| An authenticator app | Any TOTP app, such as Google Authenticator, Microsoft Authenticator or 1Password | Every account, including local test accounts, must set up MFA. |
| A browser | Chrome, Edge or Firefox | The session cookies are `Secure`; these browsers accept them on `http://localhost`. |

Ports used on `127.0.0.1`: 5173 (web app), 8000 (API), 5433 (PostgreSQL), 6380 (Redis),
1025 (Mailpit SMTP) and 8025 (Mailpit inbox). They must be free.

## 2. Environment setup (`.env`)

All configuration comes from environment variables. Locally they live in `.env` at the
repository root. `.env` is git-ignored. Never commit it, and never paste its values into
documents, tickets or chat.

1. Create it from the template (repository root):

   ```powershell
   Copy-Item .env.example .env
   ```

   On macOS or Linux, use `cp .env.example .env`.

2. Generate each secret locally and paste it in. Run these from `backend/` (after `uv sync`,
   step 3.2) so that uv provides Python:

   ```powershell
   # A password or token: the six passwords and nothing else
   uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
   # A base64 32-byte key: RATE_LIMIT_KEY_HMAC_KEY, EMAIL_LOOKUP_HMAC_KEY, FIELD_ENCRYPTION_KEYS
   uv run python -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
   ```

   Use a different value for every variable.

3. Fill in the variables by purpose. The values below are placeholders. `<...>` means a value
   you generate locally.

   | Purpose | Variable | Local value |
   |---|---|---|
   | PostgreSQL container superuser (first start only) | `POSTGRES_SUPERUSER_PASSWORD` | `<generated password>` |
   | Database login roles | `HRMS_DB_MIGRATOR_PASSWORD`, `HRMS_DB_APP_PASSWORD`, `HRMS_DB_WORKER_PASSWORD`, `HRMS_DB_AUDIT_RETENTION_PASSWORD` | `<generated password>` each |
   | Redis rate-limit user | `REDIS_RATELIMIT_PASSWORD` | `<generated password>` |
   | Environment | `APP_ENV` | `local` |
   | Logging | `LOG_LEVEL` | `INFO` |
   | Interactive API docs | `API_DOCS_ENABLED` | `true` to serve `/api/v1/docs` locally (refused in staging and production) |
   | Proxy trust | `TRUSTED_PROXY_CIDRS` | empty |
   | Database connections | `DATABASE_URL_APP` | `postgresql://hrms_app:<HRMS_DB_APP_PASSWORD>@127.0.0.1:5433/hrms` |
   | | `DATABASE_URL_WORKER` | `postgresql://hrms_worker:<HRMS_DB_WORKER_PASSWORD>@127.0.0.1:5433/hrms` |
   | | `DATABASE_URL_MIGRATOR` | `postgresql://hrms_migrator:<HRMS_DB_MIGRATOR_PASSWORD>@127.0.0.1:5433/hrms` |
   | Rate limiter | `REDIS_URL` | `redis://hrms_ratelimit:<REDIS_RATELIMIT_PASSWORD>@127.0.0.1:6380/0` |
   | | `RATE_LIMIT_KEY_HMAC_KEY` | `<generated base64 key>` |
   | Web app origin (CSRF check, invite and reset links) | `APP_BASE_URL` | `http://localhost:5173` |
   | Field encryption (TOTP secrets) | `FIELD_ENCRYPTION_KEYS` | `{"1": "<generated base64 key>"}` |
   | | `FIELD_ENCRYPTION_ACTIVE_VERSION` | `1` |
   | Breached-password check (needs internet) | `HIBP_ENABLED` | `true`, or `false` when working offline |
   | Keyed hash of attempted emails | `EMAIL_LOOKUP_HMAC_KEY` | `<generated base64 key>`, different from `RATE_LIMIT_KEY_HMAC_KEY` |
   | Worker email (Mailpit) | `SMTP_HOST` / `SMTP_PORT` / `SMTP_SECURITY` | `127.0.0.1` / `1025` / `none` |
   | | `SMTP_FROM` | `hrms@example.com` |
   | | `SMTP_USERNAME`, `SMTP_PASSWORD` | empty (set both or neither) |

   The PostgreSQL and Redis passwords are read only when their containers are first created.
   If you change them later, reset the local database (section 4.4).

## 3. Repository setup and start-up

### 3.1 Start the local services (repository root)

Start Docker Desktop and wait until it is running. Then:

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d --wait
```

This starts PostgreSQL 18, Redis and Mailpit and waits until they are healthy. The first start
creates the database login roles from `infra/db/bootstrap-roles.sql`. To stop the services
and keep the data, run `docker compose --env-file .env -f infra/compose.yaml stop`.

### 3.2 Install dependencies and apply migrations (`backend/`)

```powershell
uv sync
uv run --env-file ../.env alembic upgrade head
```

Migrations run as `hrms_migrator`. Alembic is the only migration runner, and it also installs
the Procrastinate job-queue schema. Never run `procrastinate schema --apply`.

### 3.3 Start the API (`backend/`)

```powershell
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
```

The API listens on `http://127.0.0.1:8000`. On Windows, `--loop asyncio:SelectorEventLoop` is
required because psycopg's async driver does not support the default Proactor event loop.

### 3.4 Start the worker (`backend/`, second terminal)

```powershell
uv run --env-file ../.env python -m app.worker
```

### 3.5 Start the web app (`frontend/`, third terminal)

```powershell
corepack enable
pnpm install
pnpm dev
```

### Windows shortcut

`run-backend.cmd` in the repository root runs 3.1 to 3.4 in one go. It checks that Docker is
running, starts the services, runs `uv sync` and the migrations, opens the worker in a new
window and then runs the API in the current one. Run it from the repository root with
`.\run-backend.cmd`, or double-click it. Press `Ctrl+C` to stop the API. Close the worker
window to stop the worker. Start the web app separately (3.5).

## 4. Database (PostgreSQL 18)

PostgreSQL is the only system of record: data, jobs, sessions, lockout state, audit and the
email outbox.

### 4.1 Start

Started by the Compose command in 3.1, on `127.0.0.1:5433`, database `hrms`.

### 4.2 Migrate

From `backend/`:

```powershell
uv run --env-file ../.env alembic upgrade head
uv run --env-file ../.env alembic current
```

`alembic current` prints the applied revision followed by `(head)`.

### 4.3 Check health

From the repository root:

```powershell
docker compose --env-file .env -f infra/compose.yaml ps
docker compose --env-file .env -f infra/compose.yaml exec postgres pg_isready --username postgres --dbname hrms
```

Expected: the `postgres` service shows `(healthy)`, and `pg_isready` prints
`/var/run/postgresql:5432 - accepting connections`. With the API running,
`http://127.0.0.1:8000/api/health/ready` returns `{"status":"ok"}` only when the API can reach
the database.

### 4.4 Reset the local database (local only, deletes all local data)

This deletes the local PostgreSQL volume: every account, the audit log and the job queue.
Run it only on your own machine. Never use it against a shared, staging or production
environment. Stop the API and worker first. Then, from the repository root:

```powershell
docker compose --env-file .env -f infra/compose.yaml down -v
docker compose --env-file .env -f infra/compose.yaml up -d --wait
```

Then apply the migrations (4.2) and create the test accounts again (section 9). Mailpit keeps
its messages in memory, so restarting it empties the inbox. Never use `alembic downgrade` as a
reset.

## 5. Redis

Redis holds only short-lived, HMAC-keyed rate-limit counters with a time-to-live. It is not a
system of record: it never holds data, sessions, permissions, lockout state, audit, jobs or
caches of personal data. If Redis is down, rate limiting degrades and the API stays up. For
this reason the readiness check does not include Redis.

- Start: started by the Compose command in 3.1, on `127.0.0.1:6380`, with the ACL user
  `hrms_ratelimit`.
- Check health (repository root):

  ```powershell
  docker compose --env-file .env -f infra/compose.yaml exec redis sh -c 'redis-cli --no-auth-warning --user hrms_ratelimit --pass $REDIS_RATELIMIT_PASSWORD ping'
  ```

  Expected: `PONG`. `docker compose ... ps` also shows the `redis` service as `(healthy)`.

## 6. Backend API

| What | URL |
|---|---|
| API base | `http://127.0.0.1:8000/api/v1` |
| Liveness | `http://127.0.0.1:8000/api/health/live` returns `{"status":"ok"}` |
| Readiness (database) | `http://127.0.0.1:8000/api/health/ready` returns `{"status":"ok"}` |
| Interactive API docs (only with `API_DOCS_ENABLED=true`) | `http://127.0.0.1:8000/api/v1/docs` |
| OpenAPI document (only with `API_DOCS_ENABLED=true`) | `http://127.0.0.1:8000/api/v1/openapi.json` |

The committed API contract is `api/openapi.json`. It is regenerated with a command, not served
(section 12).

Unsafe requests (POST, PUT, PATCH, DELETE) must carry `X-Requested-With: sv-web`. When the
browser sends an `Origin` header, it must equal `APP_BASE_URL`. Otherwise the API answers
`403` with `code: "csrf"`. The web app sends both, so test the API through the web app or set
these headers yourself.

## 7. Frontend

- Start (`frontend/`): `pnpm dev`
- Open: `http://localhost:5173`. Use exactly the origin in `APP_BASE_URL`. `127.0.0.1:5173` is a
  different origin, and its requests are refused by the CSRF check.
- Connection to the API: the Vite dev server forwards every `/api` request to
  `http://127.0.0.1:8000` (`frontend/vite.config.ts`). The app and the API therefore share one
  origin, as in production, and there is no CORS. The API must be running for any page beyond
  the static shell.

## 8. Worker and background jobs

- Start (`backend/`): `uv run --env-file ../.env python -m app.worker`
- What it does: every minute it sends pending emails from the outbox (`notify.dispatch_email`)
  to Mailpit. Every hour it creates audit-log partitions ahead of time
  (`audit.ensure_partitions`).
- Check it is running: its log shows `Starting worker on all queues`. About once a minute it
  shows `Job notify.dispatch_email[...] ended with status: Success`. Emails such as password
  resets reach `http://127.0.0.1:8025` within about a minute. On Windows it also logs
  `Skipping signal handling, does not work on Windows`, which is expected.
- Queue state (repository root):

  ```powershell
  docker compose --env-file .env -f infra/compose.yaml exec postgres psql -U postgres -d hrms -c "SELECT task_name, status, count(*) FROM procrastinate.procrastinate_jobs GROUP BY 1, 2"
  ```

  If the worker is not running, emails stay in the outbox with status `pending` until it
  starts. Nothing is lost.

## 9. Test sign-in

### 9.1 There are no default credentials

The application has no built-in, seeded or deterministic test login, by design
(`docs/security-architecture.md` §3.1). Every account starts as an invite. The person chooses
the password and sets up an authenticator app, and both are required. The supported way to
create the first accounts on a fresh database is the bootstrap command. It creates the first
two super admins (two different people are required, because a second super admin must
approve later role changes).

Use obviously fake addresses on the reserved `example.com` domain for local test accounts, for
example `first.admin@example.com` and `second.admin@example.com`. Never use a real Sigvitas
address locally.

### 9.2 Create the test accounts (`backend/`, once per database)

Run this after the migrations, with the services running:

```powershell
uv run --env-file ../.env python -m app.cli bootstrap-super-admins --email first.admin@example.com --email second.admin@example.com
```

Expected output (the tokens are random, and each run prints different ones):

```text
Invite links (valid for 72 hours, single use). Deliver each to its owner only:
  first.admin@example.com: http://localhost:5173/invite/<random token>
  second.admin@example.com: http://localhost:5173/invite/<random token>
```

- The invite links are printed in the terminal only. No email is sent for them.
- The command runs once per installation. Run it again and it prints
  `Refused: this installation already has super admins.` and exits with code 1. To start
  over, reset the local database (4.4).
- The links are secrets. Do not commit them or paste them into shared places.

### 9.3 Activate an account

The web app and the API must both be running.

1. Open the invite link in the browser.
2. **Password**: choose a password of 12 to 128 characters. Common passwords are refused, and
   breached ones are too when `HIBP_ENABLED=true`. A few unrelated words work well.
3. **Authenticator app**: scan the QR code with your authenticator app. On the same device,
   copy the key instead. Enter the current 6-digit code.
4. **Recovery codes**: 10 single-use recovery codes are shown once. Save them now.
5. Select **Go to your account**. You are signed in and the Security page opens.

The account is active only after all three steps. The invite link stops working once it is
used.

Your local test credentials are therefore:

| Field | Value |
|---|---|
| Email | the address you passed to the bootstrap command, for example `first.admin@example.com` |
| Password | the password you chose in step 2 |
| Second factor | the 6-digit code from your authenticator app, or one recovery code |

Keep your local test credentials in `local-logins.md` at the repository root. That file is
git-ignored for this purpose. Never commit passwords, invite links, authenticator keys or
recovery codes.

### 9.4 MFA for local testing

- When the sign-in page asks for a code, enter the current 6-digit code from the authenticator
  app.
- Codes are time-based. If correct-looking codes are refused, sync the computer clock (Windows:
  Settings > Time & language > Date & time > Sync now) and the phone clock.
- Without a phone, for local test accounts only: copy the key shown in step 3 and generate codes
  from `backend/` (pyotp is a backend dependency):

  ```powershell
  uv run python -c "import pyotp; print(pyotp.TOTP('<key from the setup screen>').now())"
  ```

  The key is as sensitive as the authenticator itself. Keep it only for local test accounts,
  only in `local-logins.md`, and never commit it.

### 9.5 Recovery codes

- On the code screen, select **Use a recovery code instead** and enter one of the saved codes.
  Each code works once.
- After a recovery-code sign-in, the session can only set up a new authenticator app. You are
  taken to `/account/mfa-enrolment`. A "A recovery code was used on your Sigvitas HRMS account"
  email arrives in Mailpit.
- Lost both the authenticator and the codes for a local test account: reset the local database
  (4.4) and bootstrap again.

### 9.6 What a super admin sees

`super_admin` has no access to employee data. After signing in, a test super admin sees the
Security page (`/account/security`: sessions, devices, authenticators, recovery codes) and can
read Settings (`/settings`).

## 10. Sign-in test checklist (browser)

Run through these with the web app, API and worker running.

- [ ] Open `http://localhost:5173`. You are sent to the sign-in page.
- [ ] Sign in with the test email and password. The code screen appears.
- [ ] Enter the 6-digit code. The Security page loads.
- [ ] On the Security page, the current session is listed and marked as this session.
- [ ] Reload the page. You stay signed in.
- [ ] Sign out. You return to the sign-in page, and opening `/account/security` sends you back to
  sign-in.
- [ ] Sign in again with password and code.
- [ ] Wrong password: the page says "The details you entered are not correct. Check them and try
  again." The same message appears for an unknown email, so it does not reveal whether the
  account exists. Repeated failures slow down and then lock the account; the
  defaults are a slowdown after 5 failures and a 15-minute lock after 10 within 15 minutes. A
  lock notice email appears in Mailpit.
- [ ] Invalid invite: open `http://localhost:5173/invite/not-a-real-token`, and open an
  already-used invite link. Both show "This invite link doesn't work".
- [ ] Password reset: sign out, select the reset link on the sign-in page and enter the test
  email. Within about a minute, "Reset your Sigvitas HRMS password" arrives at
  `http://127.0.0.1:8025`. Open its link, set a new password and sign in with it (the
  authenticator is still required). The old password is refused, and the reset link works
  only once.
- [ ] Trusted devices: after signing in, the Security page lists this browser under devices.
  Sign in again from the same browser: no "New sign-in" email is sent. Sign in from another
  browser or a private window: a "New sign-in to your Sigvitas HRMS account" email arrives in
  Mailpit. Recognizing a device never skips the authenticator code.
- [ ] Recovery code: sign in with a recovery code (9.5) and set up a new authenticator.

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| `failed to connect to the docker API` / `docker info` fails | Start Docker Desktop and wait until it says it is running, then repeat the command. `run-backend.cmd` checks this first. |
| Compose says `set POSTGRES_SUPERUSER_PASSWORD in .env` (or another variable) | `.env` is missing or the variable is empty. Create `.env` (section 2) and fill in every value. Run Compose from the repository root with `--env-file .env`. |
| API or worker fails at start-up with a settings validation error | A required variable is missing or invalid, such as a key that is not base64 or shorter than 32 bytes, or a non-`http(s)` `APP_BASE_URL`. The error names the variable. Fix it in `.env`. |
| PostgreSQL unavailable (`/api/health/ready` returns 503, or `connection refused` on 5433) | Run `docker compose --env-file .env -f infra/compose.yaml ps`. If `postgres` is not healthy, run `up -d --wait` again and read `docker compose --env-file .env -f infra/compose.yaml logs postgres`. |
| `password authentication failed for user "hrms_..."` | The role passwords in `.env` changed after the volume was created. Put the old values back, or reset the local database (4.4). |
| Redis unavailable | Check with the `ping` in section 5 and `docker compose ... logs redis`. The API keeps working with degraded rate limiting. Restart with `up -d --wait`. |
| Migration fails | Check that PostgreSQL is healthy and that `DATABASE_URL_MIGRATOR` uses `hrms_migrator` and port 5433. Read the error. A half-applied local database can be reset (4.4). Never fix it with `alembic downgrade` or `procrastinate schema --apply`. |
| Port 8000, 5173, 5433, 6380, 1025 or 8025 already in use | Find the process with `Get-NetTCPConnection -LocalPort 8000 -State Listen \| Select-Object OwningProcess`, then stop it with `Stop-Process -Id <pid>` if it is an old API or Vite instance. On macOS or Linux, use `lsof -i :8000`. Vite refuses to pick another port, because the origin must match `APP_BASE_URL`. |
| API start fails with a Proactor or event-loop error on Windows | Include `--loop asyncio:SelectorEventLoop` in the uvicorn command (3.3). |
| Sign-in fails with "This request was blocked because it did not come from the HRMS application" | The page origin differs from `APP_BASE_URL`. Open `http://localhost:5173`, not `127.0.0.1:5173`, or change both to match. |
| Signed in but immediately signed out again, or cookies missing | Use Chrome, Edge or Firefox on `http://localhost`. The `Secure` session cookies are not kept on other hosts over plain HTTP. |
| Emails (reset, notices) never arrive | The worker is not running, or SMTP is wrong. Start it (3.4) and check `SMTP_HOST=127.0.0.1`, `SMTP_PORT=1025`, `SMTP_SECURITY=none`. Then wait up to a minute and look at `http://127.0.0.1:8025`. |
| Invite link says it doesn't work | It was used, expired (72 hours) or mistyped. On a local database with no other accounts, reset (4.4) and bootstrap again. |
| Authenticator codes refused | Sync the clocks (9.4). Make sure the app entry is the one created in the latest setup. Each new setup gives a new key. |
| New password refused as breached while offline | Set `HIBP_ENABLED=false` in `.env` and restart the API. The bundled common-password list still applies. |
| `bootstrap-super-admins` prints `Refused: this installation already has super admins.` | Super admins already exist. Use the existing accounts, or reset the local database (4.4). |

## 12. Verification commands

Run the same checks as CI before committing (`docs/engineering-principles.md` §8).

Backend (`backend/`):

| Check | Command |
|---|---|
| Formatting | `uv run ruff format --check .` (apply with `uv run ruff format .`) |
| Lint | `uv run ruff check .` |
| Types | `uv run mypy .` |
| Module boundaries | `uv run lint-imports` |
| Tests (Docker must be running; starts its own containers) | `uv run pytest` |
| Dependency audit | `uv run pip-audit --strict --progress-spinner off` |
| Regenerate the API contract `api/openapi.json` | `uv run python -m scripts.export_openapi` |

Frontend (`frontend/`):

| Check | Command |
|---|---|
| Formatting | `pnpm format:check` (apply with `pnpm format`) |
| Lint (includes accessibility rules) | `pnpm lint` |
| Types | `pnpm typecheck` |
| Banned words in UI copy | `pnpm copy:check` |
| Tests | `pnpm test` |
| Build | `pnpm build` |
| Dependency audit | `pnpm audit` |
| Regenerate API types after the contract changes | `pnpm api:types` |

Security scans (repository root, with Docker, using the same images as CI):

```powershell
docker run --rm -v "${PWD}:/repo" zricethezav/gitleaks:v8.30.1 git /repo --redact --no-banner
docker run --rm -v "${PWD}:/src" -w /src semgrep/semgrep:1.178.0 semgrep scan --config p/python --config p/security-audit --metrics off --error
```

On macOS or Linux, use `"$PWD:/repo"` and `"$PWD:/src"`.

After `export_openapi` and `pnpm api:types`, `git diff -- api/openapi.json frontend/src/lib/api-schema.ts`
must be empty unless you changed the API on purpose. CI fails on a stale contract.

## 13. Project status

Current checkpoint: **M1 Checkpoint D** (complete; see `docs/m1-implementation-plan.md` §10).

Update this document in the same change whenever any of these change: local setup, commands,
ports, environment variables, the bootstrap process or the test sign-in flow.
