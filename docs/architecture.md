# Sigvitas HRMS — System Architecture

Status: Draft v0.1
Related: `architecture-decisions.md` (ADRs), `security-architecture.md`, `database-design.md`, `api-architecture.md`

## 1. Summary

A modular monolith: one FastAPI application and one React single-page application, served from the same origin, backed by PostgreSQL as the only system of record. Redis is ephemeral infrastructure used only for shared rate-limit counters (§3.1); losing it loses no data. Background work runs in a separate worker process from the same codebase, using a PostgreSQL-backed job queue. Files live in private S3-compatible object storage.

```
                 Browser (React SPA)            Future: mobile app
                        |                              |
                        |  HTTPS, same origin          |  HTTPS, bearer tokens
                        v                              v
              +---------------------------------------------+
              |  Reverse proxy (TLS, HSTS, size limits,     |
              |  coarse rate limits, static SPA files)      |
              +---------------------------------------------+
                        |  /api/*            | /ws (Phase 2+)
                        v                    v
              +---------------------------------------------+
              |  FastAPI application (API process)          |
              |                                             |
              |  platform: auth context, authorization      |
              |  engine, audit writer, errors, settings     |
              |                                             |
              |  modules: identity | access | org | people  |
              |  attendance | leave | payroll | documents   |
              |  notifications | requests | assets | audit  |
              |  reports | search | settings | (chat) (ai)  |
              +---------------------------------------------+
                  |               |               |
                  v               v               v
           +------------+   +-----------+   +-------------------+
           | PostgreSQL |   |   Redis   |   | Private object    |
           | (system of |   | ephemeral |   | storage (S3 API)  |
           | record +   |   | rate-limit|   | documents,        |
           | job queue) |   | counters  |   | payslips, exports |
           +------------+   +-----------+   +-------------------+
                  ^
                  |  same codebase, separate process
           +---------------------------------------------+
           |  Worker: notifications, email, exports,     |
           |  attendance recompute, malware scan hand-   |
           |  off, retention jobs, scheduled accruals    |
           +---------------------------------------------+
                  |
                  v
           Email provider (SMTP/API)     ClamAV (scan service)
```

## 2. Why a modular monolith

- One team, one deployable, one database transaction boundary. Audit writes must commit atomically with the change they describe; that is trivial in one process and hard across services.
- HR modules are tightly related (attendance needs shifts, leave and holidays; payroll needs attendance and leave). Splitting them early would create chatty network calls and distributed consistency problems with no scaling benefit at Sigvitas's size.
- Module boundaries are still enforced (see §4), so a module such as `chat` or `reports` can be extracted later if it needs independent scaling.

## 3. Runtime components

| Component | Technology | Role |
|---|---|---|
| Web client | React 19, TypeScript, Vite, Tailwind CSS v4, React Router (SPA mode), TanStack Query/Table, React Hook Form, Zod, React Aria Components, Motion | UI only. No business rules beyond input validation and display logic. |
| API | Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) with psycopg 3 | All business logic and all authorization. |
| Migrations | Alembic (single runner for the application schemas and the Procrastinate schema) | Forward-only in production, expand/contract. See `database-design.md` §11. |
| Worker | Procrastinate (PostgreSQL-backed jobs) in a separate process | Async and scheduled work. Jobs are enqueued in the same transaction as the business change (no lost or phantom jobs). |
| Database | PostgreSQL 18 | System of record, job queue, full-text search, future pgvector. |
| Ephemeral counters | Redis 7 (or Valkey) | Shared rate-limit and throttling counters only (§3.1). Never authoritative for anything. |
| Object storage | S3-compatible API, private buckets, server-side encryption | Documents, payslips, export files, attachments. |
| Malware scanning | ClamAV daemon (container) | Scan uploads before release. |
| Email | Transactional email provider via SMTP or API (to be chosen) | Invites, resets, notifications. |
| Reverse proxy | Caddy or nginx | TLS termination, HSTS, static assets, request size limits, first-line rate limiting. |

Rationale and alternatives for each choice are in `architecture-decisions.md`.

### 3.1 Redis: exact scope (ADR-010)

Redis is kept in the MVP for one concrete need: the API runs as two or more replicas, and rate limits must be counted across all of them. These counters change on almost every request. They have no value after a few minutes and do not belong in the system of record, its write-ahead log or its backups.

**Permitted uses (MVP):**

| Use | Key pattern | TTL | If Redis is unavailable |
|---|---|---|---|
| Per-IP login / MFA / password-reset / step-up throttling and progressive delay | `rl:auth:{purpose}:{hmac(ip)}` | ≤ 1 h | Fail closed on these auth endpoints: respond `503` with `Retry-After`. The proxy's per-IP limits and the PostgreSQL account lockout (below) remain active. |
| Per-user API rate limits (`api-architecture.md` §9) | `rl:api:{scope}:{user_id}` | ≤ 1 h | Fail open, log a warning, raise an operational alert. Proxy limits still apply. |
| Per-user limits for document downloads and export requests | `rl:dl:{user_id}`, `rl:exp:{user_id}` | ≤ 1 h | Fail closed (`503`). These are the exfiltration-sensitive paths. |

**Phase 2 candidate (decided then, not now):** cross-replica WebSocket fan-out via pub/sub. PostgreSQL `LISTEN/NOTIFY` is the alternative and is evaluated first.

**Not permitted, ever:** Redis is never the authoritative or only store for employees, attendance, leave, payroll, permissions or role assignments, sessions or tokens, audit records, security events, lockout state, documents, idempotency keys, job queues or notifications. Account lockout (`identity.users.locked_until` and failed-attempt counters) and every security event are in PostgreSQL. Effective permissions are resolved from PostgreSQL on every request. There is no application data cache in the MVP. A cache added later needs an ADR that defines invalidation, and it may never hold `personal`, `sensitive`, `restricted` or `secret` data.

**Configuration:** persistence disabled (no RDB/AOF), every key has a TTL, `maxmemory-policy volatile-ttl`, TLS, a dedicated ACL user limited to the commands the rate limiter needs, private network only. Keys contain no personal data: IPs and emails are HMAC-ed with a dedicated key before use in key names. A Redis restart resets counters, and that is the full extent of the impact.

## 4. Backend structure

```
backend/
  app/
    main.py                 # app factory, middleware, router registration
    platform/               # cross-cutting, no business logic
      config.py             # typed settings from environment
      db.py                 # engine, session, unit of work
      security/             # password hashing, tokens, crypto, step-up
      authz/                # permission catalog, policy engine, scope filters
      audit/                # audit writer (used by every module)
      errors.py             # RFC 9457 problem details
      jobs.py               # Procrastinate app
      storage.py            # object storage client, signed URLs
      logging.py            # structured logs + redaction
    modules/
      identity/             # users, credentials, sessions, MFA, invites
      access/               # roles, permissions, role assignments
      org/                  # departments, designations, locations
      people/               # employees, job history, personal & sensitive data
      attendance/           # events, days, shifts, corrections
      leave/                # types, policies, ledger, requests, holidays
      payroll/              # compensation, components, periods, payslips (Phase 2)
      documents/
      notifications/
      reports/
      audit/                # read side of audit + security events
      settings/
      search/
    workers/                # job definitions grouped by module
  migrations/               # Alembic
  tests/
```

Each module has the same internal layout:

| File | Responsibility |
|---|---|
| `router.py` | HTTP layer: parse request, call service, shape response. No SQL, no business rules. |
| `schemas.py` | Pydantic request/response models. Separate models per audience (e.g. `EmployeeSelfView`, `EmployeeTeamView`, `EmployeeHrView`). |
| `service.py` | Use cases. Calls the authorization engine, repositories, audit writer, and other modules' public services. |
| `repository.py` | SQLAlchemy queries. Always receives a scope filter from the authz engine for list queries. |
| `models.py` | SQLAlchemy models for this module's tables only. |
| `policies.py` | Resource-level rules for this module (ownership, team boundary, state checks). |
| `events.py` | Domain events this module publishes (e.g. `LeaveApproved`). |
| `public.py` | The only thing other modules may import. |

**Boundary rules** (enforced in CI with `import-linter`):

1. A module may import another module only through its `public.py`.
2. No module imports another module's `models.py` or `repository.py`.
3. `platform` imports no module.
4. Cross-module reactions go through domain events handled in-process (same transaction) or as jobs (after commit).
5. Foreign keys across module tables are allowed (one database), but writes to another module's tables are not.

## 5. Request lifecycle

1. Proxy terminates TLS, applies size limits and coarse rate limits.
2. Middleware assigns a request ID, sets security headers, starts a structured log context.
3. Authentication dependency resolves the session from the access token, loads the user, their effective permissions (resolved from PostgreSQL on every request: active role assignments within their validity window plus derived roles; there is no permission cache in the MVP) and step-up timestamp. CSRF check for unsafe methods on cookie-authenticated requests.
4. Router validates input with Pydantic (strict mode, extra fields forbidden).
5. Service calls `authz.require(actor, permission, resource)` or obtains `authz.scope_filter(actor, permission)` for lists. Denials raise a 403 (or 404 where revealing existence would leak information, see `authorization-model.md` §8).
6. Service performs the change in one unit of work: business rows, audit record, domain events, enqueued jobs — one commit.
7. Response model is chosen by the actor's field-tier permissions; Pydantic serializes only declared fields.

## 6. Data flow patterns

- **Write path is transactional.** Business change, audit record, and job enqueue commit together (transactional outbox via Procrastinate on the same connection).
- **Derived data is recomputable.** Attendance day summaries and leave balances are projections from append-only sources (events, ledger). A recompute job can rebuild them.
- **Policy is data, rules are code.** Attendance and leave rules are pure domain functions parameterized by effective-dated policy records that HR configures (`database-design.md` §4.5, §4.6). For example, `derive_day(events, shift, attendance_policy, calendar_context) -> DaySummary`. No Sigvitas-specific value (grace minutes, thresholds, leave types, accrual amounts, leave year start) appears in code. Recomputing a past day uses the policy version that was effective on that day.
- **Audit integrity is sealed asynchronously.** Audit rows commit with the business change; a single sealer job hash-chains them within about a minute, with signed, externally anchored checkpoints (`security-architecture.md` §8.1).
- **Files never pass through the browser unauthorized.** Uploads stream through the API to a quarantine prefix; the worker scans and promotes them. Downloads are signed URLs issued per request after authorization.
- **Exports are jobs.** Large reads run in the worker, write a file to private storage, and notify the requester.

## 7. Frontend architecture

```
frontend/
  src/
    app/            # providers, router, error boundaries, auth bootstrap
    design-system/  # tokens, primitives built on React Aria Components
    features/       # one folder per module: api hooks, components, routes
      attendance/
      leave/
      people/
      ...
    lib/            # api client (generated types), formatting, permissions helper
```

- API types are generated from the FastAPI OpenAPI schema (`openapi-typescript`). No hand-written duplicates of backend models.
- Server state lives in TanStack Query. No global client-state store unless a concrete need appears.
- The client calls `GET /api/v1/me` on load, receiving the user, employee summary and a list of permission keys. The UI uses them only to hide navigation and actions; every call is still authorized on the server.
- Routes that the user has no permission for are not registered in the router, so there are no dead links.
- No secrets, no API base URL constants: the SPA calls relative `/api/...` paths on its own origin.

## 8. Mobile readiness

Nothing in the design depends on browser cookies being the only credential:

- The session model issues opaque access and refresh tokens. The web client receives them as `HttpOnly` cookies; a future mobile client receives the same tokens in the response body and sends `Authorization: Bearer` (see `security-architecture.md` §3).
- All business behaviour is in the API; a mobile app is another client.
- Clock-in accepts optional device metadata (platform, app version, coarse location if policy requires it) already in MVP.
- Push notifications are a delivery channel added to the existing notification service.
- The responsive web app is the MVP mobile experience. A PWA install option can be added in Phase 2 with no architecture change. Native apps (React Native) are a Future decision.

## 9. AI readiness (Future)

The core HRMS works without AI. When an assistant is added it will be a client of the same service layer, not a new path to the data.

```
User -> AI endpoint -> LLM
                        |
                        | tool call (e.g. get_leave_balance, search_policy_docs)
                        v
              Tool adapter (runs as the requesting user)
                        |
                        v
              Existing module services -> authz engine -> database
```

Rules:

1. The assistant never receives database credentials, SQL access, or unscoped repository access.
2. Every tool is a thin wrapper around an existing service function, executed with the requesting user's actor context. If the user cannot see a salary in the UI, the tool cannot return it.
3. Tools are read-only initially. Any write (e.g. "apply leave for Friday") produces a draft the user confirms through the normal UI flow.
4. Retrieval (pgvector) stores chunk-level access metadata; the permission filter is applied in the SQL query before similarity ranking, never after. Compensation, sensitive identifiers and private messages are never embedded.
5. Content from documents and messages is untrusted input to the model (prompt injection). The model's output cannot widen permissions because the tool layer, not the model, enforces them.
6. Every AI request and tool call is audited with the actor, tools called, and resources returned.
7. Model provider, data residency and retention are decided before any HR data is sent to an external model.

A Model Context Protocol server could later expose the same tool adapter with the user's delegated token; the authorization properties are identical.

## 10. Deployment topology (to be confirmed)

Target for MVP, assuming a single region hosting close to Sigvitas users:

- Containers: `proxy`, `api` (2+ replicas), `worker` (1+), `clamav`.
- Managed PostgreSQL with automated backups and point-in-time recovery.
- Managed Redis or a container, configured per §3.1. Loss is tolerable.
- Managed object storage with versioning and server-side encryption; bucket blocks public access at the account level.
- A separate write-once (object lock, compliance mode) bucket for audit checkpoint anchors (`security-architecture.md` §8.1).
- Migrations run as a one-off job before the application rolls out (`database-design.md` §11).
- Secrets from the platform's secret manager, injected as environment variables at runtime.
- Environments: `local`, `test` (CI), `staging` (production-like, synthetic data only), `production`.

Hosting provider and region are open questions (see roadmap). If Sigvitas operates in India, an India region is the default choice for data residency and latency.

## 11. Observability

- Structured JSON logs with request ID, actor ID (not name/email), route, status, latency. A redaction filter removes tokens, passwords, and fields tagged as personal or sensitive.
- Metrics: request rates/latencies per route, job queue depth and failures, login failure rate.
- Tracing with OpenTelemetry in Phase 2.
- Error tracking must scrub request bodies and never receive personal data; self-hosted or an EU/India-region provider with a data processing agreement.
- Health endpoints: `/api/health/live` (process up), `/api/health/ready` (database and storage reachable). Redis unavailability does not make the API unready, because it degrades per §3.1, but it raises an operational alert. Neither endpoint reveals versions or configuration.
- Operational alerts specific to integrity: unsealed audit rows older than 10 minutes, checkpoint signature or anchor mismatch, audit partition missing without a retention tombstone.
