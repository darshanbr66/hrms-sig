# Production deployment

Status (2026-10-19): **proposed, not applied.** Nothing has been created in any cloud account, no
database has been migrated and nothing is deployed. This document describes the smallest
production setup that fits the existing code, the decisions still open, and the manual runbook.
Every step that creates or changes a cloud resource needs the project owner's approval first.

Hosting is ADR-023 (still pending). This proposal: the SPA on **Vercel**, the API, worker,
ClamAV and the migration job on **Render**, **PostgreSQL 18** (Render Postgres, subject to the
checks in §2), **AWS S3** for documents, exports and the write-once audit anchors, **Resend**
for email over the existing SMTP sender, and a **TLS Redis** for rate-limit counters.

## 1. What the code requires (verified in the repository)

| Area | Requirement | Where |
|---|---|---|
| Frontend | React 19 + Vite SPA, pnpm 12.8.1 (`packageManager`), Node ≥ 20.19, `pnpm build` → `frontend/dist`, client-side routing (every unknown path serves `index.html`) | `frontend/package.json`, `src/app/App.tsx` |
| Same origin | SPA at `/`, API at `/api/v1`, health at `/api/health/*`. No CORS anywhere; cookies are `__Host-` (host-only, `Secure`, `SameSite`); unsafe requests need `X-Requested-With: sv-web` and an `Origin` equal to `APP_BASE_URL` | `app/platform/csrf.py`, `modules/identity/router.py` |
| API | FastAPI on uvicorn, `app.main:app_factory`, listens on `$PORT`; liveness `/api/health/live`, readiness `/api/health/ready` (database only, §12) | `app/main.py`, `app/platform/health.py` |
| Worker | `python -m app.worker`, Procrastinate on PostgreSQL. Its schedules run inside the process (email every minute, audit sealing every minute, checkpoints hourly, verification daily, leave accrual daily, scans and exports every minute), so it must run continuously and never sleep | `app/worker.py` |
| Database | **PostgreSQL 18** exactly or later: `uuidv7()` defaults, `transaction_timeout`; extensions `citext`, `btree_gist`, `pg_trgm`; four login roles (`hrms_migrator` with `CREATEROLE`, `hrms_app`, `hrms_worker`, `hrms_audit_retention`) created by `infra/db/bootstrap-roles.sql`; migrations only through Alembic as `hrms_migrator` (27 revisions, `0001`–`0027`, forward-only in production) | `migrations/versions/`, `docs/database-design.md` §11 |
| TLS | In `staging`/`production` the processes refuse to start unless every PostgreSQL URL has `sslmode=verify-full`, `REDIS_URL` is `rediss://`, `APP_BASE_URL` and any S3 endpoint are `https`, SMTP is `tls` or `starttls`, `LOG_LEVEL` is not `DEBUG` and API docs are off | `app/platform/config.py` |
| Redis | Rate-limit counters only, TTL-bound, HMAC-ed keys; auth limits fail closed when Redis is down | `docs/architecture.md` §3.1 |
| Storage | S3 API. One private bucket for documents and exports (presigned 60 s downloads, path-style addressing); one **Object Lock (compliance)** bucket with versioning for audit anchors | `app/platform/files.py`, `app/platform/storage.py` |
| Scanning | ClamAV daemon on TCP 3310; required when deployed; unscanned files cannot be downloaded | `app/platform/scanning.py` |
| Email | SMTP from the worker's outbox (retries, nothing lost when the provider is down) | `app/platform/email.py` |
| Operators | `python -m app.cli bootstrap-super-admins` (prints secret invite links) and `audit-verify` | `app/cli.py` |

Documentation compared with the code during this review: `architecture.md` §11 said readiness
checks the database and storage, the code checks the database only (now documented as intended,
§12); `security-architecture.md` §5 said no library injects `<style>` elements, React Aria does
(two fixed rules, now allowed by hash, §8); the deployment topology in `architecture.md` §10
assumed a reverse proxy (Caddy or nginx) in front of everything, which this proposal replaces
with Vercel.

## 2. Decisions and blockers before go-live

Each of these stops a working or safe production deployment. None is solved by weakening a
check; the code refuses to start rather than run without TLS, scanning or storage.

| # | Blocker | Why | Options (owner decides) |
|---|---|---|---|
| B1 | **Client addresses behind Vercel** | Vercel forwards `/api` to Render; Render's proxy adds Vercel's egress address to `X-Forwarded-For`. Vercel publishes no fixed egress ranges, so the API cannot trust that hop and every employee appears to come from a few Vercel addresses. The per-IP sign-in rule then blocks *everyone* for 15 minutes after a handful of failures from anywhere, password resets are limited to 10 per hour for the whole company, and audit and login history record Vercel's address. | (a) **Recommended:** a code change, reviewed as security-sensitive: Vercel adds a secret header on every forwarded request (`vercel.json` `routes` with a request-header transform); the API refuses requests without it (except health checks) and, only when it matches, takes the visitor address from Vercel's `x-real-ip`. (b) Serve the SPA from the API's own Render service instead of Vercel (one origin, one proxy hop; a code and build change). (c) Pilot only, accepted risk: raise the per-IP failure threshold in Settings; the per-account lockout in PostgreSQL still applies. |
| B2 | **PostgreSQL TLS verification** | The code requires `sslmode=verify-full`. Render's internal connections use self-signed certificates and do not support `verify-full`. **Tested 2026-10-19 (phase 1, throwaway Render Postgres 18.6, Singapore):** the *external* host presents a Let's Encrypt certificate (`*.singapore-postgres.render.com`, issuer YR1, TLS 1.3); `verify-full` succeeds against the Mozilla bundle and the Windows store; plaintext is refused. `sslrootcert=system` failed from a Windows client (libpq there has no default store), so always name the bundle file. Not yet seen: verification from inside the Linux image against `/etc/ssl/certs/ca-certificates.crt` (first migration run shows it; a failure refuses to connect, it never falls back). | **Chosen:** external URLs with `sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt`, and the database IP allow list limited to the Render region's outbound addresses. |
| B3 | **Database roles on Render** | **Tested 2026-10-19 (phase 1):** Render's default user is not superuser, has `CREATEROLE` and `CREATEDB`, `createrole_self_grant` is empty; creating the four roles, the `ADMIN OPTION` grants, the trusted extensions and the migrator's schema work all succeeded (rolled back). Found: the original `CREATE DATABASE … OWNER hrms_migrator` fails for a non-superuser on PostgreSQL 16+ (`must be able to SET ROLE "hrms_migrator"`), and run as that user `REVOKE ALL ON DATABASE … FROM PUBLIC` would silently leave PUBLIC's `CONNECT`/`TEMP`. | **Fixed in `bootstrap-roles.sql`:** the administrator takes `SET` (no `INHERIT`) on `hrms_migrator` for the database statements only, revokes as the owner, and gives the grant up. `tests/integration/test_bootstrap_roles.py` bootstraps as a superuser and as a Render-like administrator, then migrates to head and checks grants; the old file reproduces the error. The persistent phase 2 run on Render was stopped by the owner and not done: the first production bootstrap is the first real run (§6 has the pre- and post-checks). |
| B4 | **Redis with TLS** | `REDIS_URL` must be `rediss://`. Render Key Value is TLS only on external connections (which need an IP allow list) and offers a single `default` user, not the restricted ACL user in `infra/redis/ratelimit.acl`. | (a) Render Key Value external URL (`rediss://`), allow list limited to the API's outbound addresses, persistence off, `maxmemory-policy volatile-ttl`. (b) A managed Redis/Valkey with TLS and ACL users in or near Singapore. |
| B5 | **Committed database dump** | `hrms_local_test-pre-0027.dump` (a local test database: fake `@example.com` accounts, password hashes, encrypted MFA secrets, sessions, audit rows) was committed in `b6c5f1c` and is on `origin/main`. Removed from tracking in the deployment commit (approved 2026-10-19) and ignored by `*.dump`; it stays in the history. | History rewriting is not allowed here (no force push), so treat everything in it as exposed: never reuse local keys or passwords anywhere, and confirm the GitHub repository is private. |
| B6 | **Anchor bucket retention** | Compliance-mode Object Lock cannot be shortened by anyone once set. Its default retention is the audit retention plus one year (`security-architecture.md` §8.1), and the audit retention period is still a Sigvitas input (`database-design.md` §9). | Sigvitas decides `AUDIT_LOG_RETENTION_MONTHS` and `SECURITY_EVENTS_RETENTION_MONTHS`; the bucket is created after that. |
| B7 | **Region and data residency** | Render's closest region to India is Singapore; Render has no India region. `architecture.md` §10 defaults to an India region if Sigvitas operates there. | Sigvitas accepts Singapore for compute and database (S3 can stay in `ap-south-1`), or chooses another host. |
| B8 | **Backups beyond PITR** | Product requirement: PITR of at least 7 days (Render: Pro workspace) **and** encrypted daily backups kept 35 days, restore tested quarterly. Render's logical exports are manual and kept 7 days. | A scheduled encrypted `pg_dump` to S3 needs a read-only backup role (a migration and grants change for approval); until then, PITR (7 days) plus a manual export before every migration. |

Smaller items to settle: the workspace plans and costs (§16); a staging environment (the
documented process migrates staging before production; this proposal starts with production
only for a small internal pilot, which is a deviation for the owner to accept); log retention
of 30 days (Render's retention depends on the workspace plan; otherwise a log stream).

## 3. Topology

```
Employee browser ──HTTPS──> Vercel  (https://<hrms domain>)
                              │  /assets, /index.html, SPA routes: static files, security headers
                              │  /api/*  ── external rewrite (30 s limit) ──┐
                                                                            ▼
                              Render, region singapore, private network
                              ├─ hrms-api     (web, Docker image, /api/health/ready)
                              ├─ hrms-worker  (background worker, same image)
                              ├─ hrms-clamav  (private service, clamav/clamav, TCP 3310)
                              ├─ hrms-migrate (cron job, run by hand, same image)
                              └─ hrms-db      (PostgreSQL 18)
                              External services over TLS:
                              ├─ AWS S3: <files bucket> (private), <anchor bucket> (Object Lock)
                              ├─ Redis with TLS (rate-limit counters)
                              └─ Resend SMTP (smtp.resend.com:465)
```

The browser only ever talks to `https://<hrms domain>`: cookies stay first-party and host-only,
the CSRF origin check compares against that origin, and there is no CORS. Vercel's 30-second
limit on forwarded requests is enough: long work (exports, scans) runs in the worker. Uploads
(at most 10 MB) pass through Vercel; test one on a slow connection during the pilot.

Why not the documented proxy (Caddy or nginx): Vercel serves the SPA and its headers; Render
terminates TLS for the API. The request-size limits that proxy would have enforced (1 MB JSON,
20 MB uploads) are enforced by the API itself for uploads; a general JSON body limit at the edge
is not available on this topology and is listed as a risk.

## 4. Repository files

| File | Purpose |
|---|---|
| `render.yaml` | Render Blueprint: database, API, worker, ClamAV, migration job, with secrets as `sync: false` or Render-generated. Validated against Render's schema. |
| `backend/Dockerfile`, `backend/.dockerignore` | One image for API, worker, migrations and operator commands; non-root; runtime dependencies only; uvicorn without its access log (tokens travel in invite and reset paths) and without proxy-header rewriting. |
| `frontend/vercel.json` | Build settings, the `/api` rewrite to Render, SPA fallback, security headers and the SPA's Content-Security-Policy, caching rules. Validated against Vercel's schema. |
| `frontend/scripts/check-deploy-config.mjs` | Runs before every Vercel build: refuses the placeholder API host, a non-https or path-changing rewrite, and any `VITE_` variable whose name looks like a secret. |
| `frontend/playwright.csp.config.ts`, `frontend/e2e/production-csp.e2e.ts` | `pnpm test:csp`: builds the SPA and runs it under the headers from `vercel.json`, failing on any CSP violation. |
| `infra/production.env.example` | Every production setting by service, with descriptions and no values. |

## 5. Creating the Render services (after approval)

1. Choose the workspace plan (Pro for a 7-day PITR window, §2 B8) and confirm the region (B7).
2. Settle B2 and B3 on a throwaway PostgreSQL 18 instance, then delete it.
3. In Render: **New → Blueprint**, connect the GitHub repository, branch `main`, file
   `render.yaml`. Review every service, plan and region before applying.
4. Render asks for each `sync: false` value: enter them from the secret store (§7). Leave the
   generated ones (`RATE_LIMIT_KEY_HMAC_KEY`, `EMAIL_LOOKUP_HMAC_KEY`) to Render.
5. Do not start the worker or the API until the database is bootstrapped and migrated (§6, §11).
   Every service has auto-deploy off; deploy them by hand in the order of §11.
6. Record each service's outbound IP addresses (Render dashboard → service → Outbound) for the
   database and Redis allow lists, if B2 (a) or B4 (a) is chosen.

## 6. Database

**Provision.** Render Postgres 18 (`hrms-db` in `render.yaml`, default database `hrms_admin`,
external access closed by `ipAllowList: []`).

**Bootstrap the roles (once).** From an operator machine whose address is temporarily on the
allow list (or the database's Render shell), as the default Render user, with four new random
passwords from the secret store:

```bash
psql "<admin connection URL, sslmode=require or verify-full>" \
  -v db_name=hrms \
  -v migrator_password="$HRMS_DB_MIGRATOR_PASSWORD" \
  -v app_password="$HRMS_DB_APP_PASSWORD" \
  -v worker_password="$HRMS_DB_WORKER_PASSWORD" \
  -v audit_retention_password="$HRMS_DB_AUDIT_RETENTION_PASSWORD" \
  -f infra/db/bootstrap-roles.sql
```

Before it, as the same user: `SHOW createrole_self_grant;` must be empty (otherwise the
administrator would inherit every role it creates; stop). After it, this must return no rows
(the administrator can no longer act as an HRMS role), and the database ACL must show no
`PUBLIC` entry (`\l hrms`):

```sql
SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
  AND r.rolname LIKE 'hrms_%' AND (m.inherit_option OR m.set_option);
```

If the script stops part-way, do not repair it by hand: delete the database instance and start
again on a fresh one.

Then remove the operator address from the allow list. The four role URLs have the form
`postgresql://<role>:<password>@<host>:5432/hrms?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt`
(the image ships the system trust store; see B2 for which host).

**Never** restore a local or test dump into production, never run `scripts/` seeding or
`procrastinate schema --apply` against it, and never run `alembic downgrade` there (CLAUDE.md
rule 20). Production starts empty: the only data the system creates itself is the permission
catalog and roles from the migrations.

**Backups.** Confirm PITR is active (Render: Recovery page) before every migration and record
the time. Take a logical export from the Recovery page before each migration (kept 7 days).
Restore drill, quarterly and before go-live: PITR into a new instance, point a temporary copy of
the API at it with production settings, confirm sign-in and recent records, then delete it.

## 7. Configuration and secrets

`infra/production.env.example` lists every variable per service. Rules:

- Generate every key and password for production only, on a trusted machine
  (`infra/production.env.example` has the commands). Nothing from a local `.env`, the committed
  dump or any test database is ever reused.
- Keep a copy of `FIELD_ENCRYPTION_KEYS` and the audit signing key in the company's secret store,
  outside Render: losing the field key makes every enrolled authenticator unreadable; losing the
  signing key ends checkpoint signing (rotation keeps the old public key listed).
- Each service holds only its own credentials: the migrator URL only in `hrms-migrate`, the
  signing key and SMTP key only in `hrms-worker`, the field keys only in `hrms-api`.
- Vercel holds no application secrets. `ENABLE_EXPERIMENTAL_COREPACK=1` lets Vercel use the pnpm
  version pinned in `package.json` (confirm Vercel supports pnpm 12; otherwise set the install
  command to a supported version and record it).
- Settings errors at startup name the variable and the rule, never the value
  (`hide_input_in_errors`), so a misconfiguration does not print secrets into the logs.

## 8. Vercel

1. New project from the GitHub repository; **Root Directory** `frontend`. `vercel.json` sets the
   install, build and output settings (`pnpm install --frozen-lockfile`,
   `node scripts/check-deploy-config.mjs && pnpm build`, `dist`).
2. Replace `__RENDER_API_HOST__` in `frontend/vercel.json` with the Render API's host (for example
   the `onrender.com` host shown for `hrms-api`, or a custom API host) in a reviewed commit. The
   build refuses to run until then.
3. Production domain: add `<hrms domain>` (a subdomain of the company domain), set its DNS as
   Vercel instructs, and wait for the certificate. Set `APP_BASE_URL=https://<hrms domain>` on
   `hrms-api` and `hrms-worker`. Unsafe requests from any other origin are refused, so the
   `*.vercel.app` address cannot sign in or change anything.
4. Preview deployments use the same `vercel.json`, so they forward to the production API. Turn on
   Vercel **Deployment Protection** for previews, or disable previews, until a staging API exists.
5. Headers (`vercel.json`): HSTS with preload, the SPA's CSP from `security-architecture.md` §5
   (no storage origin is needed: downloads are top-level navigations to presigned URLs), plus the
   two `sha256` hashes for React Aria's fixed `<style>` rules (touch-action on pressable elements;
   overscroll on iOS while a dialog is open); `X-Frame-Options`, `nosniff`, `no-referrer`,
   Permissions-Policy, COOP and CORP. `/api/*` keeps the API's own stricter headers and is never
   cached (`no-store` from the API, and rewrite caching off). Hashed assets are cached for a year;
   `index.html` revalidates every time.
6. Run `pnpm test:csp` after any dependency upgrade: a React Aria change to those `<style>` rules
   fails it.

## 9. Email (Resend)

1. In Resend, add a sending domain, preferably a subdomain used only for the HRMS (for example
   `hrms.<company domain>` or the one Resend suggests), in a region near the users.
2. Add the DNS records Resend shows (SPF and DKIM; the return-path subdomain's MX/TXT) at the DNS
   provider, and a DMARC record for the domain (`p=none` with reporting first, tightened after
   the pilot). Wait until Resend shows the domain as verified.
3. Create an API key with **sending access only**, restricted to that domain. It is the SMTP
   password (`SMTP_PASSWORD`), held only by `hrms-worker`.
4. `SMTP_FROM` uses the verified domain, for example `Sigvitas HRMS <hrms@<sending domain>>`.
5. Render blocks outbound SMTP on free instances only; the worker runs on a paid plan. Resend
   also accepts ports 2465/2587 if 465 is ever blocked.
6. Failure handling is in the code: emails are written to the outbox in the same transaction as
   the change and sent by the worker with retries, so a Resend outage delays mail and loses none.
   Invite and reset links are built from `APP_BASE_URL`, never from a request.

## 10. Storage (AWS S3) and scanning

**Files bucket** (`FILES_BUCKET`, region per B7, for example `ap-south-1`): Block Public Access
on (account and bucket), default encryption SSE-S3 or SSE-KMS, Object Ownership "bucket owner
enforced" (no ACLs), versioning on, no bucket policy granting public or cross-account access. An
IAM user for the HRMS with only:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::<files bucket>/*"
    }
  ]
}
```

Presigned downloads are signed with these keys and live 60 seconds. Exports are deleted by the
application; a lifecycle rule that expires noncurrent versions after a period Sigvitas chooses
keeps versioning from growing without bound.

**Anchor bucket** (`AUDIT_ANCHOR_BUCKET`, created after B6): Object Lock enabled at creation,
default retention **COMPLIANCE**, days = audit retention + 1 year, versioning on (required by
Object Lock), Block Public Access on. A separate IAM user with only:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectRetention"],
      "Resource": "arn:aws:s3:::<anchor bucket>/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
      "Resource": "arn:aws:s3:::<anchor bucket>"
    }
  ]
}
```

No delete, no retention or legal-hold changes, no bucket configuration. Never the same keys as
the files bucket.

**ClamAV** (`hrms-clamav`): a private service on Render's private network only (no public
address; clamd has no authentication, so it must never be exposed). The first start downloads
signatures (minutes); until it answers, uploads stay "being checked" and cannot be downloaded.
Pin the image by digest before go-live.

## 11. Deploying (every release)

Order from `database-design.md` §11. Nothing deploys on its own (auto-deploy is off).

1. Confirm CI passed on the commit and that `vercel.json` names the right API host.
2. Confirm PITR is active; record the time; take a logical export.
3. **Migrate:** `hrms-migrate` → Deploy the commit, then **Trigger Run**. Read its log: it must end
   at the expected head (`0027` today). If it fails, stop: the old worker and API keep running on
   the old schema (expand/contract).
4. **Worker:** deploy the commit to `hrms-worker`; its log shows every periodic task registered and
   no errors. Never start a worker or API on a newer commit than the migrated schema: they do not
   check the schema revision at startup (a startup check is proposed in §17).
5. **API:** deploy the commit to `hrms-api`; Render waits for `/api/health/ready` before switching
   traffic.
6. **Frontend:** promote the Vercel production deployment of the same commit.
7. Smoke tests (§13, first part).

## 12. Health checks, logs and alerts

- `/api/health/live` (process up) and `/api/health/ready` (database reachable) reveal nothing but
  `{"status":"ok"}` or a problem response. Readiness deliberately excludes Redis and object
  storage: their failures degrade specific features (rate limits per §3.1; uploads, downloads and
  exports return 503), and taking the API out of rotation would turn them into a full outage.
- Logs are JSON on stdout with request IDs and route templates; secrets and personal fields are
  redacted (`app/platform/logging.py`). uvicorn's own access log is off. Platform request logs
  (Vercel, and Render's HTTP request logs on Pro workspaces) record raw paths, including the
  token in `/invite/<token>` and `/api/v1/auth/password-reset/<token>`: limit who can read them.
  Tokens are single-use and expire (lifetimes in Settings).
- Alerts to set up before inviting employees: Render service failures and restarts, `hrms-api`
  health check failures, failed worker jobs in its log, the `audit.chain_mismatch` security
  event (the worker also emails it to super admins and auditors), Resend bounces and failures, Render Postgres disk and connection usage, and the audit alerts the worker already
  emails to super admins and auditors. Keep application logs 30 days
  (`security-architecture.md` §7): use a Render log stream if the workspace keeps less.
- Error tracking (Sentry or similar) is not in the code; adding it needs a dependency decision and
  must scrub bodies and personal data (`architecture.md` §11).

## 13. Smoke tests and the first administrator

**After the first deployment, before inviting anyone:**

1. `curl -si https://<hrms domain>/api/health/ready` → `200 {"status":"ok"}`, with HSTS and the
   API's CSP. `curl -si https://<hrms domain>/` → the SPA with the SPA's CSP and HSTS.
2. `curl -si -X POST https://<hrms domain>/api/v1/auth/login` → `403` with `"code": "csrf"`.
3. `curl -si https://<render api host>/api/v1/me` → `401` (direct access works but is
   unauthenticated; after B1 (a) it is refused without Vercel's header).
4. Browser: the sign-in page loads with no console errors (no CSP violations).

**Create the first two super admins** (two different people who do not need HR data, ADR-025),
from Render's **Shell** on `hrms-api`, not as a job (job output is kept in the logs, and the
links are secrets):

```bash
python -m app.cli bootstrap-super-admins --email <first person> --email <second person>
```

Deliver each link to its owner over a trusted channel. Each sets a password and enrols an
authenticator (MFA is mandatory); the command refuses to run again once super admins exist.
Recovery: a super admin who loses their authenticator is reset by the other super admin (the
administrator MFA reset, `security-architecture.md` §3); if both are locked out, the total
lock-out recovery procedure is still to be written (§15). There is no backdoor account.

**Pilot acceptance (a small group, before inviting all employees):**

- [ ] Both super admins signed in with MFA; sign-out works; a new sign-in needs MFA again.
- [ ] HR admin invited through the app; the email arrives from the verified domain with a link to
      `https://<hrms domain>/invite/...`; activation requires an authenticator.
- [ ] Account security page: login history shows each person's own public address (not a
      Vercel or Render address; this checks B1).
- [ ] Permissions: an employee cannot open HR or admin pages or call their APIs (403); a manager
      sees only their team; a super admin sees no personal or salary data without an approved
      elevation.
- [ ] Attendance: clock in, break, clock out; the day shows the right times in the local zone.
- [ ] Leave: request, approve, balance changes; the requester gets the notification email.
- [ ] Documents: upload a PDF; it is "being checked", then downloadable; the download link stops
      working after 60 seconds; another employee cannot download it; the EICAR test file is
      blocked.
- [ ] Exports: an export completes and downloads.
- [ ] Password reset email arrives; the old sessions end.
- [ ] `python -m app.cli audit-verify` (Render Shell on `hrms-worker`) reports OK for both streams
      the next day.
- [ ] Logs show no errors, no tokens and no personal data; alerts fire when tested.

Only after the owner approves the pilot are the remaining employees invited.

## 14. Rollback

- **Frontend:** Vercel → Deployments → promote the previous production deployment (instant).
- **API and worker:** Render → service → Deploys → redeploy the previous successful deploy. The
  schema stays: every migration is expand/contract, so the previous code runs on the new schema.
- **Migration failure:** the run stops at the failed revision inside its transaction (and its
  `lock_timeout` of 5 s); fix forward with a new revision. Never `alembic downgrade` in
  production.
- **Bad data or a destructive migration:** PITR to a new instance at the recorded time, verify,
  then point the services' database URLs at it (the roles and passwords come with the restore).
  Expect data written after that time to be lost; announce it.
- **Secrets leaked:** rotate the affected key or password (field keys by adding a new version and
  keeping the old one; signing key by adding the new public key), revoke all sessions, review
  the audit log.

## 15. Incident basics

Who to call, in order: the project owner, then the second engineer. First actions: keep the
evidence (do not delete logs or audit rows), stop the bleeding (rotate the leaked credential,
disable the account, block the path), then restore service. Detailed runbooks (restore, key
rotation, revoking sessions, MFA reset, break-glass, audit chain mismatch, Redis outage) are
still to be written (`engineering-principles.md` §11).

## 16. Costs and plan limits to confirm

Prices change; confirm on the providers' pricing pages before approval. Categories:

| Service | What is paid for | Limits that matter |
|---|---|---|
| Render workspace | Seats; Pro needed for a 7-day PITR window | Log retention per plan |
| `hrms-api`, `hrms-worker` | Instance plan (`render.yaml`: 0.5 CPU, 512 MB each) | Paid plans do not sleep; free ones do and block SMTP. Watch memory. |
| `hrms-clamav` | 2 CPU / 4 GB plan and a 2 GB disk | ClamAV needs ~1.5 GB and more while reloading signatures; a disk prevents zero-downtime deploys of that service only |
| `hrms-migrate` | Per-minute while running | — |
| Render Postgres 18 | Instance plan and storage | Connections per plan; PITR 3 days (Hobby) or 7 days (Pro) |
| Redis with TLS | Plan of the chosen provider | TLS and allow list (B4) |
| Vercel | Plan (Hobby is for non-commercial use; a company system needs Pro) | 30 s limit on forwarded requests; request logs show paths |
| AWS S3 | Storage, requests, data transfer out (downloads) | Object Lock retention cannot be shortened |
| Resend | Monthly email volume | Domain verification; sending rate |

## 17. Proposed follow-ups (not done, need approval)

- B1 (a): origin secret and trusted visitor address (security-sensitive code, second review).
- A startup or readiness check that the database is at the code's Alembic head, so a worker or
  API never runs against an older schema.
- A read-only backup role and an encrypted daily `pg_dump` to S3 (B8).
- A staging environment with synthetic data, migrated before production.
- Error tracking with scrubbing; uptime checks on `/api/health/ready` from outside.
- An edge request-size limit for JSON bodies (Vercel has none configurable for rewrites).
