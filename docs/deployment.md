# Production deployment

Status (2026-10-20): **proposed, not applied.** Nothing is deployed, and the live Atlas database
`hrms` has not been migrated, written to or configured by this work. This document describes the
production setup that fits the code after ADR-030, the decisions still open, and the manual
runbook. Every step that creates or changes a cloud resource needs the project owner's approval
first.

Architecture (ADR-030, decided by the project owner on 2026-10-20): the SPA on **Vercel**, the
API on **Render** (one Docker web service that also runs the scheduled jobs), **MongoDB Atlas**
(`Cluster0`, database `hrms`) for all data including files, and **Resend's HTTPS API** for email.
No PostgreSQL, Redis, AWS or S3. The earlier PostgreSQL proposal is in this file's Git history
(commit `f442d13`).

## 1. What the code requires (verified in the repository)

| Area | Requirement | Where |
|---|---|---|
| Frontend | React 19 + Vite SPA, pnpm (`packageManager`), Node ≥ 20.19, `pnpm build` → `frontend/dist`, client-side routing (every unknown path serves `index.html`) | `frontend/package.json` |
| Same origin | SPA at `/`, API at `/api/v1`, health at `/api/health/*`, file downloads at `/api/v1/files/<token>`. No CORS; cookies are `__Host-`; unsafe requests need `X-Requested-With: sv-web` and an `Origin` equal to `APP_BASE_URL` | `app/platform/csrf.py`, `app/platform/files.py` |
| API | FastAPI on uvicorn, `app.main:app_factory`, listens on `$PORT`; liveness `/api/health/live`, readiness `/api/health/ready` (MongoDB answers and, with `RUN_JOBS_IN_API=true`, the jobs are running: their heartbeat is under 16 minutes old) | `app/main.py`, `app/platform/health.py`, `app/platform/jobs.py` |
| Jobs | Email outbox, audit sealing, checkpoints, verification, witness and retention, exports, leave accrual, grant expiry, document scans. With `RUN_JOBS_IN_API=true` they run inside the API process on their own database connections (`MONGODB_WORKER_URI`); otherwise `python -m app.worker` | `app/platform/jobs.py`, `app/worker.py` |
| Database | MongoDB 8 as a **replica set** (multi-document transactions; Atlas clusters always are). Collections, `$jsonSchema` validators and indexes are applied by `python -m app.cli migrate`, as the migration user | `app/platform/schema.py`, `migrations/__init__.py` |
| TLS | In `staging`/`production` the processes refuse to start unless every MongoDB URI uses TLS (`mongodb+srv://`, or `tls=true`) without `tlsInsecure`/`tlsAllowInvalidCertificates` and names a database user, `APP_BASE_URL` is `https`, `LOG_LEVEL` is not `DEBUG` and API docs are off | `app/platform/config.py` |
| Refused at start | Production without `AUDIT_WITNESS_EMAILS`; staging and production with audit retention (`MONGODB_AUDIT_RETENTION_URI` or a retention period) while `RUN_JOBS_IN_API=true`; a worker or retention URI that signs in as the same database user as `MONGODB_URI`; the database name `hrms` anywhere but production | `app/platform/config.py` |
| Rate limits | Sliding-log counters in MongoDB with a TTL index; authentication, download and export limits fail closed (`503`) when the database is unreachable | `app/platform/ratelimit.py` |
| Files | GridFS bucket `files` in the same database, scoped to the organization; 60-second signed same-origin download links (`DOWNLOAD_LINK_HMAC_KEY`) | `app/platform/files.py` |
| Scanning | ClamAV on TCP 3310 while uploads are on. Without it set `DOCUMENT_UPLOADS_ENABLED=false`: uploads are refused (`503 documents.uploads_disabled`) and nothing is stored | `app/platform/scanning.py` |
| Email | Resend HTTPS API (`EMAIL_TRANSPORT=resend`, `RESEND_API_KEY`, `EMAIL_FROM`); SMTP is refused outside `local` and `test`. Mail is written to the outbox in the business transaction and sent by the jobs with retries | `app/platform/email.py` |
| Operators | `python -m app.cli migrate`, `create-organization`, `bootstrap-super-admins`, `audit-verify`, `audit-export-checkpoints`; every command that changes data needs `--confirm-database <MONGODB_DATABASE>` | `app/cli.py` |

## 2. Decisions and blockers before go-live

None of these is solved by weakening a check: the code refuses to start rather than run without
TLS, without a separate worker user, or with uploads on and no scanner.

| # | Blocker | Why | Options (owner decides) |
|---|---|---|---|
| B1 | **Client addresses behind Vercel** | Vercel forwards `/api` to Render; Vercel publishes no fixed egress ranges, so the API cannot trust that hop and every employee appears to come from a few Vercel addresses. The per-address sign-in rule can then block everyone after a handful of failures, and login history records Vercel's address. | (a) **Recommended:** a secret header added by Vercel on forwarded requests, checked by the API before it trusts `x-real-ip` (security-sensitive code change, second review). (b) Serve the SPA from the Render service instead. (c) Pilot only, accepted risk: raise "Failed sign-in attempts from one network address" in Settings; the per-account lockout still applies. |
| B2 | **Atlas custom roles** | Least privilege at the database (the API cannot update or remove audit records, attendance events, the ledger or histories; only the retention user removes audit records) exists only once the custom roles from `scripts/mongo_roles.py` are created and each process connects as its own user. The roles are tested against a local MongoDB 8 replica set; they have **not** been created or verified on Atlas. The Atlas free and Flex tiers restrict some features; confirm in Atlas that custom roles with collection-level actions can be created on `Cluster0`'s tier. | Create the roles and users (§4), then verify with the checks in §4. If the tier cannot hold them, upgrade the cluster or accept, in writing, that append-only is enforced by the application only. |
| B3 | **Atlas tier, backups and storage** | The free tier (M0) has 512 MB of storage shared by records, audit and GridFS files, and no backups or point-in-time restore. Product requirement: PITR of at least 7 days and encrypted daily backups kept 35 days, restore tested quarterly. | A dedicated tier (M10 or above) with Cloud Backup and continuous backup before real employee data is entered. Until then: a pilot with no real personal data only, uploads off. |
| B4 | **Audit witness recipients** | The S3 Object Lock anchors are gone. The daily witness email of the signed checkpoint heads protects against truncation of the audit trail only if the mailboxes in `AUDIT_WITNESS_EMAILS` belong to someone outside the HRMS administrators' control (an external auditor, a mailbox at another provider). It detects, it does not prevent; see ADR-030. Production refuses to start without at least one recipient. A head is witnessed only after its stream's checkpoint chain verifies; otherwise the email names the stream instead. | Name the recipients (the code only checks that the list is not empty). Optionally, an operator runs `audit-export-checkpoints` weekly to storage the database administrators cannot change. |
| B5 | **Jobs inside the API** | One web service, no paid worker: the audit signing key, the Resend key and the worker's database user live in the internet-facing process (`security-architecture.md` §6.4 kept them apart). The audit retention user, the only one that can remove audit records, is **not** allowed there: with `RUN_JOBS_IN_API=true`, staging and production refuse `MONGODB_AUDIT_RETENTION_URI`, `AUDIT_LOG_RETENTION_MONTHS` and `SECURITY_EVENTS_RETENTION_MONTHS`, so **audit retention is off** and every audit record is kept. On Render's free plan the service sleeps after about 15 minutes idle, and the jobs (email, sealing, witness) stop with it. | Plan `starter` or above for `hrms-api` (`render.yaml`), so it never sleeps. A separate Background Worker (`RUN_JOBS_IN_API=false`, `python -m app.worker`) restores the separation when funded; only that worker may then hold the retention user and run retention. |
| B6 | **Atlas network access** | Render's outbound addresses are shared ranges per region, not addresses of this service alone. An Atlas IP access list of those ranges still admits other Render customers to the login prompt; authentication and TLS remain the real controls. `0.0.0.0/0` is not acceptable. | The Render region's outbound ranges (Render dashboard → service → Connect → Outbound) on the Atlas access list, plus the operator's address only while migrating. Private endpoints need a dedicated tier. |
| B7 | **Region and data residency** | Render has no India region (Singapore is closest); the Atlas cluster's region is whatever `Cluster0` was created in. | Confirm both regions and keep them the same or adjacent. |
| B8 | **Committed database dump** | `hrms_local_test-pre-0027.dump` (fake local accounts, password hashes, encrypted MFA secrets) was committed in `b6c5f1c` and stays in the history. | Treat it as exposed: never reuse local keys or passwords; confirm the GitHub repository is private. |

Module-specific: office location verification (ADR-031) is off unless `LOCATION_CHECKS_ENABLED=true` (default `false`, pinned to `false` in `render.yaml`). Setting it in production is prohibited until the evidence and review retention period is decided and its purge implemented, the privacy notice is approved by the designated privacy/compliance owner, and the one-office pilot is ready (`module-acceptance.md` §15.1). While it is `false`, no office check can be created and none applies; no application role can change it.

Smaller items: workspace plans and costs (§16); a staging environment (a separate Atlas database
such as `hrms_staging`, never `hrms`); log retention of 30 days.

## 3. Topology

```
Employee browser ──HTTPS──> Vercel  (https://<hrms domain>)
                              │  /assets, /index.html, SPA routes: static files, security headers
                              │  /api/*  ── external rewrite (30 s limit) ──┐
                                                                            ▼
                              Render, one Docker web service
                              └─ hrms-api  (API + scheduled jobs, RUN_JOBS_IN_API=true,
                                            /api/health/ready)
                              External services over TLS:
                              ├─ MongoDB Atlas Cluster0, database hrms
                              │    hrms_app (requests), hrms_worker (jobs),
                              │    migrator (operator only); no retention user (B5)
                              └─ Resend HTTPS API (api.resend.com:443)
```

The browser only ever talks to `https://<hrms domain>`: cookies stay first-party and host-only,
the CSRF check compares against that origin, file downloads are same-origin paths, and there is
no CORS.

**Jobs inside the API, and what that costs.** The jobs start in the API's lifespan, at most one
runner per process, and stop on shutdown. Each task claims its cron slot through a lease document
in `job_leases`, so two instances, or a restart during a run, never run one slot twice; an
interrupted slot is claimed again once its lease expires. Outbox messages, exports and scans are
claimed one by one, so a retry does not resend what was sent. The jobs use their own database
connections (`MONGODB_WORKER_URI`). That may be the same user and URI as `MONGODB_URI`; the
user then holds the worker's privileges for request handling too (§4). What it does not give: the separation of keys between the internet-facing process and the worker (B5), and
jobs on a service that sleeps.

The jobs cannot stop silently. Any error while connecting, building the scheduler or running it
is logged (`jobs.scheduler_failed`) and everything is restarted after 30 seconds. The scheduler
beats a heartbeat on every 15-second tick and after every task; `/api/health/ready` answers 503
once the heartbeat is older than a lease plus a minute (16 minutes), so Render takes the service
out of rotation and restarts it. If the runner's task ends without a shutdown, it is logged as
`jobs.runner_stopped` (critical), readiness fails at once and the process sends itself SIGTERM,
so Render restarts it rather than serving requests without email, audit sealing or the witness.

## 4. Atlas database users and roles

Each process connects as its own database user (Atlas → Database Access). Generate each password
for production only.

| User | Role | Used by |
|---|---|---|
| `hrms_migrator` | built-in `readWrite@hrms` and `dbAdmin@hrms` | `app.cli migrate`, from the operator machine only; never stored in Render |
| `hrms_app` | custom `hrms_app` | `MONGODB_URI` on `hrms-api`; `create-organization` and `bootstrap-super-admins` |
| `hrms_worker` | custom `hrms_worker` | `MONGODB_WORKER_URI` on `hrms-api` (the jobs); `audit-verify` |
| `hrms_audit_retention` | custom `hrms_audit_retention` | `MONGODB_AUDIT_RETENTION_URI`, only on a separate worker once a retention period is set; never on `hrms-api` (B5). Create it only then |

`MONGODB_URI` and `MONGODB_WORKER_URI` may name the same user with the same URI. That user needs
the custom `hrms_worker` role, which holds every `hrms_app` privilege; the API's requests then run
with the worker's privileges. Separate `hrms_app` and `hrms_worker` users remain the narrower
choice.

The custom roles are generated from the code, so production gets exactly the roles the tests use:

```powershell
cd backend
uv run python -m scripts.mongo_roles --database hrms > ..\.local-secrets\atlas-roles.json
```

Create each role in Atlas → Database Access → Custom Roles (or through the Atlas Administration
API) with the privileges from that file. Then verify, before the first deploy, as each user from
the operator machine (a short `mongosh` session):

- `hrms_app`: inserting into `attendance_events` after migration succeeds; `updateOne` and
  `deleteOne` on `attendance_events` and `audit_log` are refused (`Unauthorized`); `drop` of any
  collection is refused.
- `hrms_worker`: writes to `audit_chain_links` and `job_leases` succeed; `deleteOne` on
  `audit_log` is refused.
- `hrms_audit_retention`: nothing outside the audit collections.

Record the result. Until all of these pass on Atlas, do not describe the audit trail or the
append-only collections as protected at the database (ADR-030).

## 5. Creating the Render service (after approval)

1. Settle B2, B3, B5 and B7.
2. Render: **New → Blueprint**, repository branch `main`, file `render.yaml`. It defines one web
   service, `hrms-api`; review its plan and region before applying.
3. Enter each `sync: false` value from the secret store (§7). Leave the generated keys
   (`RATE_LIMIT_KEY_HMAC_KEY`, `EMAIL_LOOKUP_HMAC_KEY`, `DOWNLOAD_LINK_HMAC_KEY`) to Render.
4. Do not deploy until the database is migrated (§6). Auto-deploy is off.
5. Add the service's outbound ranges to the Atlas access list (B6).

## 6. Migrations

`python -m app.cli migrate` creates missing collections, sets each collection's validator and
creates declared indexes, then applies numbered data migrations not yet recorded in
`schema_migrations`. It never drops a collection or an index. It stops, changing nothing, if
existing documents do not match a new validator or an index exists with another definition. Runs
serialize on a lease, so two operators cannot migrate at once.

From the operator machine, with its address temporarily on the Atlas access list, from
`backend/` in PowerShell:

```powershell
$env:APP_ENV = "production"
$env:MONGODB_DATABASE = "hrms"
$env:MONGODB_URI = Read-Host "hrms_migrator URI" -AsSecureString |
  ForEach-Object { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) }
uv run python -m app.cli migrate --status
uv run python -m app.cli migrate --confirm-database hrms
uv run python -m app.cli migrate --status
Remove-Item Env:MONGODB_URI
```

Run it before the first deploy and before every deploy whose commit adds a migration or changes
a model. Never run tests, `run-backend.cmd`, seeding or a restore of a local database against
`hrms`; local and test settings refuse that name. Then remove the operator address from the
access list.

**Backups** (dedicated tier only, B3): confirm continuous backup is on and record the time before
every migration. Restore drill, quarterly and before go-live: restore to a new cluster, point a
temporary copy of the API at it, confirm sign-in and recent records, then delete it.

## 7. Configuration and secrets

`infra/production.env.example` lists every variable. Rules:

- Generate every key and password for production only (commands in that file). Nothing from a
  local `.env`, `.local-secrets/` or any test database is reused.
- Keep copies of `FIELD_ENCRYPTION_KEYS` and the audit signing key in the company's secret store
  outside Render: losing the field key makes every enrolled authenticator unreadable; losing the
  signing key ends checkpoint signing (rotation keeps the old public key listed).
- `MONGODB_WORKER_URI` is required when deployed and may be the same URI as `MONGODB_URI` (§4).
- The migrator URI is never stored in Render.
- Vercel holds no application secrets.
- Settings errors at startup name the variable and the rule, never the value.

## 8. Vercel

1. New project from the GitHub repository; **Root Directory** `frontend`. `vercel.json` sets
   install, build and output (`pnpm install --frozen-lockfile`,
   `node scripts/check-deploy-config.mjs && pnpm build`, `dist`).
2. `frontend/vercel.json` forwards `/api/*` to the API's `onrender.com` host
   (`hrms-sig-api.onrender.com`). The build refuses a placeholder host, a host without https and
   an `/api` rewrite that drops the prefix.

   The temporary frontend-first deployment (Vercel deployed before the Render service existed,
   with `HRMS_FRONTEND_FIRST=true`) is over and its mode is removed: the build now refuses while
   `HRMS_FRONTEND_FIRST` is set in the Vercel project at all, so make sure it is deleted there.
3. Production domain: add `<hrms domain>`, set its DNS, wait for the certificate, then set
   `APP_BASE_URL=https://<hrms domain>` on `hrms-api`. Unsafe requests from any other origin are
   refused, so `*.vercel.app` cannot sign in.
4. Previews forward to the production API: turn on Deployment Protection, or disable previews,
   until a staging API exists.
5. Headers (`vercel.json`): HSTS, the SPA's CSP with React Aria's two style hashes,
   `X-Frame-Options`, `nosniff`, `no-referrer`, Permissions-Policy, COOP, CORP. `/api/*` keeps the
   API's own headers and is never cached. Downloads are same-origin, so the CSP needs no storage
   origin.
6. Run `pnpm test:csp` after any dependency upgrade.
   **Sleeping API.** While `hrms-api` runs on a free instance it sleeps when idle (B5). The sign-in
   page checks `/api/health/ready` through this rewrite when it opens and shows "Connecting to
   HRMS…" until it passes, then "Connected to HRMS"; after about 90 seconds without an answer it
   stops and offers Retry. It sends the sign-in request only after a passed check and never
   repeats it automatically. No separate wake-up endpoint, secret or monitoring service exists,
   and none is needed; a paid instance (B5) removes the wait.
7. Hobby is for non-commercial use only; a company system needs Pro.

## 9. Email (Resend)

1. In Resend, add a sending domain used only for the HRMS (for example `hrms.<company domain>`).
2. Add the SPF, DKIM and return-path records Resend shows, and a DMARC record (`p=none` with
   reporting first). Wait until Resend shows the domain as verified.
3. Create an API key with **sending access only**, restricted to that domain: `RESEND_API_KEY` on
   `hrms-api`.
4. `EMAIL_FROM` on the verified domain, for example `Sigvitas HRMS <hrms@<sending domain>>`.
5. The API key travels over HTTPS (port 443), so Render's block on outbound SMTP ports does not
   apply.
6. Failures: messages wait in the outbox and are retried; a Resend outage delays mail and loses
   none. Resend's answer is logged by status only, never with the message.

## 10. Files and scanning

Documents and exports are stored in GridFS in `hrms` (`files.files`, `files.chunks`), named by
random keys, each tagged with its organization. Downloads go through the API: it authorizes and
audits, then returns a same-origin link valid for 60 seconds, signed with
`DOWNLOAD_LINK_HMAC_KEY`. Exports are removed after 24 hours by the jobs.

GridFS shares the database's storage limit and backups (B3); it is not write-once storage.

ClamAV is not part of this deployment: `DOCUMENT_UPLOADS_ENABLED=false`, so uploads and new
versions are refused and the UI says why; lists, categories and exports still work. Turning
uploads on needs a ClamAV daemon reachable only privately from the API (clamd has no
authentication), `CLAMAV_HOST`, and enough database storage.

## 11. Deploying (every release)

1. Confirm CI passed on the commit and `vercel.json` names the right API host.
2. Record the backup time (B3).
3. Migrate (§6).
4. Deploy the commit to `hrms-api`; Render waits for `/api/health/ready`. Its log shows the jobs
   starting. Never deploy a commit newer than the migrated database.
5. Promote the Vercel production deployment of the same commit.
6. Smoke tests (§13, first part).

## 12. Health checks, logs and alerts

- `/api/health/live` and `/api/health/ready` return only `{"status":"ok"}` or a problem response.
- Logs are JSON on stdout with request IDs and route templates; secrets and personal fields are
  redacted. uvicorn's access log is off. Platform request logs (Vercel, Render) record raw paths,
  including invite and reset tokens and `/api/v1/files/<token>`: limit who can read them. Tokens
  are single-use or expire within 60 seconds.
- Alerts before inviting employees: Render service failures and restarts, health check failures,
  failed jobs in the log (`jobs.task_failed`, `jobs.scheduler_failed`, `jobs.runner_stopped`,
  `health.jobs_unhealthy`), `audit.witness_withheld`, the `audit.chain_mismatch` security event
  (also emailed to super admins and auditors), Resend bounces, Atlas storage, connection and
  replication alerts.

## 13. Smoke tests and the first administrator

**After the first deployment, before inviting anyone:**

1. `curl -si https://<hrms domain>/api/health/ready` → `200 {"status":"ok"}` with HSTS.
2. `curl -si -X POST https://<hrms domain>/api/v1/auth/login` → `403` with `"code": "csrf"`.
3. `curl -si https://<render api host>/api/v1/me` → `401`.
4. Browser: the sign-in page loads with no CSP violations.

**Create the organization and its first two super admins** (two people who do not need HR data,
ADR-025), from the operator machine as `hrms_app`, never as a logged job (the links are secrets):

```powershell
$env:APP_ENV = "production"; $env:MONGODB_DATABASE = "hrms"; $env:APP_BASE_URL = "https://<hrms domain>"
# $env:MONGODB_URI = the hrms_app URI, entered as in §6
uv run python -m app.cli create-organization --name "Sigvitas" --slug sigvitas --confirm-database hrms
uv run python -m app.cli bootstrap-super-admins --organization sigvitas --email <first> --email <second> --confirm-database hrms
Remove-Item Env:MONGODB_URI
```

Deliver each link over a trusted channel. Each person sets a password and enrols an
authenticator; the command refuses to run again once super admins exist. There is no backdoor
account.

**Pilot acceptance** (a small group, no real personal data while B3 is open):

- [ ] Both super admins signed in with MFA; sign-out works; a new sign-in needs MFA again.
- [ ] An HR admin invited through the app; the email arrives from the verified domain; activation
      requires an authenticator.
- [ ] Login history shows each person's own address (checks B1).
- [ ] Permissions: an employee gets 403 on HR and admin APIs; a manager sees only their team; a
      super admin sees no personal data without an approved elevation.
- [ ] Attendance, leave (request, approve, notification email), exports (download link expires
      after 60 seconds), password reset (old sessions end).
- [ ] Uploads are refused with "Document uploads are turned off until virus scanning is set up".
- [ ] The next day: the witness email arrived at every `AUDIT_WITNESS_EMAILS` mailbox, and
      `python -m app.cli audit-verify --witness <saved lines>` (as `hrms_worker`) reports OK.
- [ ] Logs show no errors, tokens or personal data.

## 14. Rollback

- **Frontend:** Vercel → promote the previous production deployment.
- **API:** Render → Deploys → redeploy the previous successful deploy. Migrations only add
  collections, validators, indexes and idempotent data changes, so the previous code runs on the
  migrated database, unless a validator was tightened in a way the older code violates; such a
  change ships only after the code that satisfies it.
- **Migration failure:** it stops before changing anything that does not match; fix forward.
- **Bad data:** restore to a new cluster from backup (B3), verify, point the URIs at it. Data
  written after the restore point is lost; announce it.
- **Secrets leaked:** rotate the key or password (field keys by adding a version; signing key by
  adding the new public key), revoke all sessions, review the audit log.

## 15. Incident basics

Who to call, in order: the project owner, then the second engineer. Keep the evidence (do not
delete logs or audit records), stop the bleeding (rotate the credential, disable the account),
then restore service. Runbooks for restore, key rotation, session revocation, MFA reset,
break-glass and audit chain mismatch are still to be written (`engineering-principles.md` §11).

## 16. Costs and plan limits to confirm

Prices change; confirm on the providers' pricing pages.

| Service | What is paid for | Limits that matter |
|---|---|---|
| Render `hrms-api` | Instance plan (`starter` in `render.yaml`) | Free instances sleep (jobs stop) |
| MongoDB Atlas | Cluster tier, backup | M0: 512 MB, no backups, feature limits (B2, B3) |
| Vercel | Pro for a company system | 30 s limit on forwarded requests |
| Resend | Monthly volume | Verified domain; sending rate |

## 17. Proposed follow-ups (not done, need approval)

- B1 (a): origin secret and trusted visitor address (security-sensitive, second review).
- A readiness check that the database's recorded migration version matches the code.
- A separate Background Worker when funded (B5).
- A staging database (`hrms_staging`) migrated before production.
- Error tracking with scrubbing; external uptime checks on `/api/health/ready`.
- Rewriting `docs/database-design.md`, `architecture.md` §3 and `security-architecture.md` §8.1
  for MongoDB (ADR-030 lists where they are out of date).
