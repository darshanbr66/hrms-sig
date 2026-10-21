# Local development and test sign-in

This is the single guide for running Sigvitas HRMS on a developer machine and signing in from a
browser. Everything here is for local development only. Never use these steps, example addresses
or generated values for staging or production, and never use real Sigvitas credentials.

Commands are shown for Windows PowerShell, the main development platform. Where macOS or Linux
differ, the difference is noted. Run each command from the folder named above it.

## 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop (Docker Engine with Compose v2) | Current | Runs MongoDB 8 (a one-member replica set) and the Mailpit mail catcher. The backend tests also start their own MongoDB container. Atlas is never needed locally. |
| [uv](https://docs.astral.sh/uv/) | Current | Installs and manages Python 3.13 for the backend (`backend/.python-version`). You do not need to install Python separately. |
| Node.js | 20.19 or later (`frontend/package.json` `engines`) | |
| pnpm | 12.8.1, through corepack (`frontend/package.json` `packageManager`) | Run `corepack enable` once; corepack then provides the pinned pnpm. |
| Git | Current | |
| An authenticator app | Any TOTP app, such as Google Authenticator, Microsoft Authenticator or 1Password | Every account, including local test accounts, must set up MFA. |
| A browser | Chrome, Edge or Firefox | The session cookies are `Secure`; these browsers accept them on `http://localhost`. |

Ports used on `127.0.0.1`: 5173 (web app), 8000 (API), 27017 (MongoDB), 1025 (Mailpit SMTP),
8025 (Mailpit inbox), and 3310 (ClamAV, only with the `scan` profile). They must be free.

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
   # A base64 32-byte key: RATE_LIMIT_KEY_HMAC_KEY, EMAIL_LOOKUP_HMAC_KEY,
   # DOWNLOAD_LINK_HMAC_KEY, FIELD_ENCRYPTION_KEYS
   uv run python -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
   # The audit checkpoint signing key pair: prints AUDIT_SIGNING_KEY_ID,
   # AUDIT_SIGNING_PRIVATE_KEY and AUDIT_VERIFY_PUBLIC_KEYS, ready to paste
   uv run python -m scripts.generate_audit_signing_key
   ```

   Use a different value for every variable.

3. Fill in the variables by purpose. The values below are placeholders. `<...>` means a value
   you generate locally.

   | Purpose | Variable | Local value |
   |---|---|---|
   | Environment | `APP_ENV` | `local` |
   | Logging | `LOG_LEVEL` | `INFO` |
   | Interactive API docs | `API_DOCS_ENABLED` | `true` to serve `/api/v1/docs` locally (refused in staging and production) |
   | Proxy trust | `TRUSTED_PROXY_CIDRS` | empty |
   | Database | `MONGODB_URI` | `mongodb://127.0.0.1:27017/?replicaSet=rs0&directConnection=true` (the Compose MongoDB; no user locally) |
   | | `MONGODB_DATABASE` | `hrms_local`. **Never `hrms`**: that is the production database, and local and test settings refuse it |
   | Jobs | `RUN_JOBS_IN_API` | `true` (the jobs run inside the API, as on Render); `false` with a separate worker (8) |
   | | `MONGODB_WORKER_URI`, `MONGODB_AUDIT_RETENTION_URI` | empty locally (the jobs use `MONGODB_URI`) |
   | Rate limiter | `RATE_LIMIT_KEY_HMAC_KEY` | `<generated base64 key>` |
   | Download links | `DOWNLOAD_LINK_HMAC_KEY` | `<generated base64 key>` |
   | Web app origin (CSRF check, invite and reset links) | `APP_BASE_URL` | `http://localhost:5173` |
   | Field encryption (TOTP secrets) | `FIELD_ENCRYPTION_KEYS` | `{"1": "<generated base64 key>"}` |
   | | `FIELD_ENCRYPTION_ACTIVE_VERSION` | `1` |
   | Breached-password check (needs internet) | `HIBP_ENABLED` | `true`, or `false` when working offline |
   | Keyed hash of attempted emails | `EMAIL_LOOKUP_HMAC_KEY` | `<generated base64 key>`; the three HMAC keys must all differ |
   | Email | `EMAIL_TRANSPORT` | `smtp` (Mailpit). `resend` sends real email and is what deployed environments use |
   | | `EMAIL_FROM` | `HRMS <hrms@example.com>` |
   | | `RESEND_API_KEY` | empty locally (required with `resend`) |
   | | `SMTP_HOST` / `SMTP_PORT` / `SMTP_SECURITY` | `127.0.0.1` / `1025` / `none` |
   | | `SMTP_USERNAME`, `SMTP_PASSWORD` | empty (set both or neither) |
   | Audit checkpoint signing (jobs) | `AUDIT_SIGNING_KEY_ID`, `AUDIT_SIGNING_PRIVATE_KEY`, `AUDIT_VERIFY_PUBLIC_KEYS` | the three lines printed by `scripts.generate_audit_signing_key` |
   | Audit witness | `AUDIT_WITNESS_EMAILS` | empty, or a fake address; locally the witness goes to Mailpit |
   | Audit retention | `AUDIT_LOG_RETENTION_MONTHS`, `SECURITY_EVENTS_RETENTION_MONTHS` | empty (nothing is removed locally) |
   | Document uploads | `DOCUMENT_UPLOADS_ENABLED` | `false` (uploads refused); `true` needs ClamAV |
   | Virus scanning (ClamAV) | `CLAMAV_HOST` / `CLAMAV_PORT` | empty / `3310`; `127.0.0.1` with the `scan` profile |

   Documents and exports are stored in GridFS in the local database, so there is no bucket to
   create.

## 3. Repository setup and start-up

### Windows shortcut (recommended)

From the repository root:

```powershell
.\run-backend.cmd
```

It runs 3.1 to 3.4 in one window (the steps are in `run-backend.ps1`; the `.cmd` file lets you
double-click it): it checks that Docker is running and that no API (port 8000) or separate worker
is running, starts MongoDB and Mailpit, runs `uv sync`, checks the target database
(`scripts.check_local_target`), applies the migrations, then runs the API with the scheduled jobs
inside it (`RUN_JOBS_IN_API=true`, as on Render). When `CLAMAV_HOST` is set in `.env` it also
starts ClamAV (the `scan` profile). Press `Ctrl+C` to stop. Start the web app separately (3.5).

With no arguments it uses the database `hrms_local` on the local MongoDB. Other forms:

```powershell
.\run-backend.cmd -Database hrms_acceptance          # another local database
.\run-backend.cmd -Database hrms_dev_darshan -Atlas  # a database of its own on the cluster in MONGODB_URI
```

`-Atlas` uses `MONGODB_URI` from `.env` and is only for a separate development database. The
script and the settings both refuse `hrms`, the production database, and the script never drops
or resets a database. The database is set explicitly for the migrations and the API, so variables
left in the window cannot redirect them. The window title shows the database
(`HRMS API (hrms_local)`); an account exists only in the database where it was created.

### 3.1 Start the local services (repository root)

```powershell
docker compose -f infra/compose.yaml up -d --wait
```

This starts MongoDB (`mongo:8.0`, replica set `rs0`, `127.0.0.1:27017`, no authentication,
reachable from this machine only) and Mailpit, and waits until MongoDB is the replica set's
primary. The first start initiates the replica set. The application's units of work are
multi-document transactions, which a standalone MongoDB refuses, so the replica set is required.
To stop the services and keep the data: `docker compose -f infra/compose.yaml stop`.

ClamAV, which scans uploaded documents, runs only with the `scan` profile because it needs
about 1.5 GB of memory and downloads its virus signatures on first start (several minutes):

```powershell
docker compose -f infra/compose.yaml --profile scan up -d --wait
```

Then set `DOCUMENT_UPLOADS_ENABLED=true` and `CLAMAV_HOST=127.0.0.1`. Without ClamAV, keep uploads
off: they are refused, and nothing unscanned is stored.

### 3.2 Install dependencies and apply migrations (`backend/`)

```powershell
uv sync
uv run --env-file ../.env python -m scripts.check_local_target --database hrms_local
uv run --env-file ../.env python -m app.cli migrate --confirm-database hrms_local
```

`check_local_target` stops unless `APP_ENV` is `local` or `test`, `MONGODB_DATABASE` is the name
given and is not `hrms`, `MONGODB_URI` is on this machine, and the server is a replica set
primary. It prints no URI or credential.

`migrate` creates the collections, their validators and indexes, and applies the numbered data
migrations. It never drops anything, and running it again changes nothing. `--confirm-database`
must repeat `MONGODB_DATABASE`. `migrate --status` only reports.

### 3.3 Start the API (`backend/`)

```powershell
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
```

The API listens on `http://127.0.0.1:8000`. With `RUN_JOBS_IN_API=true` its log shows
`jobs.worker_started` with the task names.

### 3.4 A separate worker (optional, `backend/`, second terminal)

Only with `RUN_JOBS_IN_API=false` in `.env`:

```powershell
uv run --env-file ../.env python -m app.worker
```

Never run both: `run-backend.cmd` refuses to start while a separate worker runs.

### 3.5 Start the web app (`frontend/`, another terminal)

```powershell
corepack enable
pnpm install
pnpm dev
```

## 4. Database (MongoDB)

MongoDB is the only data store: records, sessions, lockout state, rate-limit counters, the email
outbox, job leases, audit and files (GridFS). Every company's records carry their organization
(`organization_id`), and every query is scoped to it.

### 4.1 Start

Started by the Compose command in 3.1, on `127.0.0.1:27017`, replica set `rs0`. Databases are
created on first use; `hrms_local` by default.

### 4.2 Migrate

From `backend/`:

```powershell
uv run --env-file ../.env python -m app.cli migrate --confirm-database hrms_local
uv run --env-file ../.env python -m app.cli migrate --status
```

### 4.3 Check health

From the repository root:

```powershell
docker compose -f infra/compose.yaml ps
docker compose -f infra/compose.yaml exec mongo mongosh --quiet --eval "rs.status().members[0].stateStr"
```

Expected: the `mongo` service shows `(healthy)`, and `mongosh` prints `PRIMARY`. With the API
running, `http://127.0.0.1:8000/api/health/ready` returns `{"status":"ok"}` only when the API can
reach the database.

### 4.4 Reset the local database (local only, deletes all local data)

Stop the API first. To drop one local database (for example `hrms_local`), from the repository
root:

```powershell
docker compose -f infra/compose.yaml exec mongo mongosh --quiet hrms_local --eval "db.dropDatabase()"
```

To delete every local database, remove the volume: `docker compose -f infra/compose.yaml down -v`,
then start again (3.1). Either way, migrate again (4.2) and create the organization and test
accounts again (section 9). Mailpit keeps its messages in memory, so restarting it empties the
inbox. Never run either command against Atlas.

## 5. Rate limits

Rate-limit counters are short-lived MongoDB documents (`rate_limit_counters`) holding an
HMAC-ed key, request times and an expiry; a TTL index removes them. They hold no personal data.
If the database cannot be reached, the general API limits fail open, and the authentication
limits fail closed: sign-in, step-up, password reset, invite activation and MFA re-enrolment
answer `503 temporarily-unavailable`, which the web app shows as "Sign-in is temporarily
unavailable. Try again in a minute." Locally that means MongoDB is not running (3.1).

The Compose services have no restart policy. After Docker Desktop or the computer restarts,
they stay stopped until you run the Compose command in 3.1 again (or `run-backend.cmd`).

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
- The navigation lists only the pages the signed-in account's permissions reach;
  `docs/roles-and-permissions.md` lists them per role.

## 8. Scheduled jobs

- Where they run: inside the API with `RUN_JOBS_IN_API=true` (the default of `run-backend.cmd`),
  or in `python -m app.worker` with `false`. Each task has a lease in `job_leases`, so a slot runs
  once even if both were started.
- What they do: every minute they send pending emails from the outbox (`notify.dispatch_email`)
  to Mailpit, record expired access requests and ended elevations (`access.expire_grants`;
  access already stops at the end time without it), seal new audit records into the hash chain
  (`audit.seal`), build queued exports and, with uploads on, scan documents. Hourly they write
  signed audit checkpoints (`audit.checkpoint`); daily they verify recent audit records
  (`audit.verify_recent`), send the audit witness (`audit.witness`, only with
  `AUDIT_WITNESS_EMAILS`), accrue leave (`leave.accrue`) and run audit retention
  (`audit.retention`, which removes nothing unless a retention period is set); monthly they
  verify everything (`audit.verify_full`).
- Check they run: the log shows `jobs.worker_started`. Emails such as password resets reach
  `http://127.0.0.1:8025` within about a minute.
- Task state (repository root):

  ```powershell
  docker compose -f infra/compose.yaml exec mongo mongosh --quiet hrms_local --eval "db.job_leases.find({}, {last_slot: 1, failures: 1, last_error_at: 1}).toArray()"
  ```

  If the jobs are not running, emails stay in the outbox with status `pending`. Nothing is lost.

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

Run this after the migrations, with the services running. First the organization (a tenant;
there is no API to create one), then its two super admins:

```powershell
uv run --env-file ../.env python -m app.cli create-organization --name "Local test company" --slug local-test --confirm-database hrms_local
uv run --env-file ../.env python -m app.cli bootstrap-super-admins --organization local-test --email first.admin@example.com --email second.admin@example.com --confirm-database hrms_local
```

`run-backend.cmd` prints both commands for the database it started. If `.env` does not set
`MONGODB_DATABASE=hrms_local`, set `$env:MONGODB_DATABASE` first, as the script shows.

Expected output (the tokens are random, and each run prints different ones):

```text
Invite links (valid for 72 hours, single use). Deliver each to its owner only:
  first.admin@example.com: http://localhost:5173/invite/<random token>
  second.admin@example.com: http://localhost:5173/invite/<random token>
```

- The invite links are printed in the terminal only. No email is sent for them.
- The command runs once per organization. Run it again and it prints
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
Security page (`/account/security`: sessions, devices, authenticators, recovery codes), can
read Settings (`/settings`) and sees Access requests (`/admin/access-requests`): request a
time-bound role, approve or reject another super admin's request, end temporary access,
break-glass to `system_admin` for up to an hour, and acknowledge another super admin's
break-glass. Each step asks for an authenticator code (step-up). Testing an approval needs both
test super admins signed in, for example in two browser profiles. Notification emails reach
Mailpit.

The bootstrapped super admins have no employee record, so their dashboard shows access
requests, recent sign-ins and shortcuts, and no attendance, holidays or profile.

The dashboard shows each widget only to holders of its permission (the API decides every read):

| Role | Dashboard |
|---|---|
| `super_admin` | Access requests waiting, recent sign-ins, how to get temporary HR access, shortcuts to Users and Access requests. No employee, attendance or leave data. |
| `hr_admin` | People figures (employed today, on notice, joining in the future, former, joined in the last 30 days) with headcounts by department and location, recent joiners, organization set-up progress, attendance today, leave waiting for a decision, access requests, shortcuts to Employees, Organization, Leave approvals, Leave overview, Leave balances and Leave types. |
| `hr` | As `hr_admin` without organization set-up, access requests and the Leave types shortcut. |
| `manager` (derived) | The same people figures and attendance for their team only, their own clock, leave and holidays, and leave waiting for their decision. |
| `employee` (derived) | Their own clock, leave, holidays and sign-ins; no figures about others. |

Self-service widgets (clock, My leave, holidays) appear only for an account linked to an employee
record, which the bootstrapped super admins are not.

To try the HR screens locally (organization, employees, holiday calendars, team attendance, leave), a
super admin asks for temporary `hr_admin` access on Access requests and the other super admin
approves it (both super admins must be active: activate the second one from its invite first); the elevated account then sees Organization, Employees, Holiday calendars and Team
attendance until the elevation ends. A typical first run: add a location (with its IANA time
zone), a department and a designation; add an employee whose work email is an address you can
read in Mailpit; on the employee's record, open **HRMS account** and send the invite (this also
needs `user.invite`, which `hr_admin` holds); activate the account from the email; sign in as
that employee to clock in and out and see the dashboard as an employee. Create a holiday
calendar for the current year, add holidays and assign the location to see them on the
employee's dashboard. Use obviously fake names and `dev.example`-style addresses only.

To try leave, with the elevated `hr_admin` account: open **Leave types**
(`/setup/leave-types`), add a test leave type (every rule is a yes or no you choose) and a policy
version for the employee's location (leave year start, optionally days given each leave year).
Use **Allocate a leave year**, or record an opening balance on **Leave balances**. Make a second
test employee the first one's manager (a job change on the record), invite both, and sign in as
the employee: **My leave** shows the balance and **Request leave** previews how the days count
before sending. Sign in as the manager: **Leave approvals** lists the request, and its page
approves it (or rejects it, with a note). Emails to the manager and the employee reach Mailpit.
The values you enter are test configuration, not Sigvitas policy.

If an account already exists for a person (for example invited before their record), link it
from the record: **HRMS account**, **Link an existing account**. The account's email must be the
record's work email; a reason and an authenticator code are asked for.

An administrator's MFA reset (`POST /users/{id}/mfa-reset`, `system_admin`) emails a
re-enrolment link to Mailpit. The link opens `/mfa-reenrolment/<token>`, which asks for the
password and then a new authenticator, and shows new recovery codes.

## 10. Sign-in test checklist (browser)

Run through these with the web app and the API (with its jobs) running.

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
| API fails at start-up with a settings validation error | A required variable is missing or invalid, such as a key that is not base64 or shorter than 32 bytes, two HMAC keys with the same value, or a non-`http(s)` `APP_BASE_URL`. The error names the variable, never its value. Fix it in `.env`. |
| `MONGODB_DATABASE=hrms is the production database` | Local and test runs refuse `hrms`. Use `hrms_local` (or another name) in `.env` or `-Database`. |
| `Refused: --confirm-database ... does not match MONGODB_DATABASE` | The confirmation must repeat the database the command will change. Check which database `.env` or `$env:MONGODB_DATABASE` names. |
| `check_local_target` stops: not a replica set primary | The local MongoDB is still starting, or was started without `--replSet`. Run `docker compose -f infra/compose.yaml up -d --wait` and check 4.3 shows `PRIMARY`. |
| MongoDB unavailable (`/api/health/ready` returns 503, or `ServerSelectionTimeoutError`) | Run `docker compose -f infra/compose.yaml ps`. If `mongo` is not healthy, run `up -d --wait` again and read `docker compose -f infra/compose.yaml logs mongo`. |
| Sign-in says "Sign-in is temporarily unavailable. Try again in a minute." (API answers `503`, log shows `ratelimit.store_unavailable`) | The rate-limit counters cannot be written because MongoDB is not reachable; the sign-in limits fail closed by design (section 5). Start the services (3.1). The API does not need a restart. |
| `migrate` stops naming a collection whose documents do not match its validator | Data written by an older version of the code no longer fits the model. On a local database, reset it (4.4). Never loosen a validator to get past it. |
| Sign-in fails for an account that worked before, and the security events show `login.failed` with `"known_user": false` | The API is running against a different local database from the one the account was created in. Restart with `.\run-backend.cmd -Database NAME`; the window title shows the database in use. |
| Sign-in says the email "is not a valid email address" for an `@example.test` account | The `.test` domain is a special-use name that address validation refuses. Use accounts on `example.com` (section 9.2). |
| Port 8000, 5173, 27017, 1025 or 8025 already in use | Find the process with `Get-NetTCPConnection -LocalPort 8000 -State Listen \| Select-Object OwningProcess`, then stop it with `Stop-Process -Id <pid>` if it is an old API or Vite instance. On macOS or Linux, use `lsof -i :8000`. Vite refuses to pick another port, because the origin must match `APP_BASE_URL`. |
| Sign-in fails with "This request was blocked because it did not come from the HRMS application" | The page origin differs from `APP_BASE_URL`. Open `http://localhost:5173`, not `127.0.0.1:5173`, or change both to match. |
| Signed in but immediately signed out again, or cookies missing | Use Chrome, Edge or Firefox on `http://localhost`. The `Secure` session cookies are not kept on other hosts over plain HTTP. |
| Start-up fails with `AUDIT_VERIFY_PUBLIC_KEYS must list the public key of AUDIT_SIGNING_PRIVATE_KEY` | The three audit key values do not belong together. Generate a new set with `uv run python -m scripts.generate_audit_signing_key` and replace all three. Existing local checkpoints then fail verification against the new key unless the old public key stays listed in `AUDIT_VERIFY_PUBLIC_KEYS` under its old ID. |
| Emails (reset, notices) never arrive | The jobs are not running (`RUN_JOBS_IN_API=false` and no worker), or SMTP is wrong. Check `EMAIL_TRANSPORT=smtp`, `SMTP_HOST=127.0.0.1`, `SMTP_PORT=1025`, `SMTP_SECURITY=none`, then wait up to a minute and look at `http://127.0.0.1:8025`. |
| An upload answers "Document uploads are turned off until virus scanning is set up" | `DOCUMENT_UPLOADS_ENABLED=false`, the default. Start ClamAV (3.1) and set `DOCUMENT_UPLOADS_ENABLED=true` and `CLAMAV_HOST=127.0.0.1`. |
| Invite link says it doesn't work | It was used, expired (72 hours) or mistyped. On a local database with no other accounts, reset (4.4) and bootstrap again. |
| Authenticator codes refused | Sync the clocks (9.4). Make sure the app entry is the one created in the latest setup. Each new setup gives a new key. |
| New password refused as breached while offline | Set `HIBP_ENABLED=false` in `.env` and restart the API. The bundled common-password list still applies. |
| `bootstrap-super-admins` prints `Refused: this installation already has super admins.` | Super admins already exist in that organization. Use the existing accounts, or reset the local database (4.4). |

## 12. Verification commands

Run the same checks as CI before committing (`docs/engineering-principles.md` §8).

Backend (`backend/`):

| Check | Command |
|---|---|
| Formatting | `uv run ruff format --check .` (apply with `uv run ruff format .`) |
| Lint | `uv run ruff check .` |
| Types | `uv run mypy .` |
| Module boundaries | `uv run lint-imports` |
| Tests (Docker must be running; starts its own MongoDB replica set container, never Atlas) | `uv run pytest` |
| Dependency audit | `uv run pip-audit --strict --progress-spinner off` |
| Regenerate the API contract `api/openapi.json` | `uv run python -m scripts.export_openapi` |
| Verify the local audit chain and checkpoints (read-only; exit code 1 on any problem; `--witness FILE` also checks saved witness lines) | `uv run --env-file ../.env python -m app.cli audit-verify` |
| Build the production image (API with jobs, worker, operator commands; `docs/deployment.md` §1) | `docker build -t hrms-backend .` |

Frontend (`frontend/`):

| Check | Command |
|---|---|
| Formatting | `pnpm format:check` (apply with `pnpm format`) |
| Lint (includes accessibility rules) | `pnpm lint` |
| Types | `pnpm typecheck` |
| Banned words in UI copy | `pnpm copy:check` |
| Tests | `pnpm test` |
| Browser layout tests (Playwright, Chromium; starts its own Vite on port 5180 and answers every API call with fake data, so no database or account is used; first run on a new machine: `pnpm exec playwright install chromium`) | `pnpm test:e2e` |
| Build | `pnpm build` |
| Production build under the production headers and CSP from `vercel.json` (builds first; Chromium) | `pnpm test:csp` |
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

Current state: **M1 Checkpoints A–F** complete; **Checkpoint G** (frontend) delivered together
with the first slices of M2 (organization and employee records, then organization management,
account linking and department heads on 2026-10-11), M3 (attendance clock events, without
shifts or policies) and M4 (holiday calendars, then leave management on 2026-10-11),
then on 2026-10-15 notifications, shifts, attendance policies and corrections, leave accrual
and year end, documents with ClamAV scanning, and exports. On 2026-10-20 the backend moved to
MongoDB (ADR-030); the PostgreSQL migrations are replaced by `app.cli migrate`. See
`docs/m1-implementation-plan.md` §12, and `docs/roles-and-permissions.md` for what each role
can open after signing in.

Update this document in the same change whenever any of these change: local setup, commands,
ports, environment variables, the bootstrap process or the test sign-in flow.
