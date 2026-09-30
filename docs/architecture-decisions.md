# Sigvitas HRMS — Architecture Decisions

Status: v0.2, after the Phase 0 architecture review and correction pass (2026-09-30). Superseded ADRs stay in the file with a pointer to the replacement.

Format: context → decision → alternatives considered → consequences.

## Status index

| ADR | Title | Status |
|---|---|---|
| 001 | Modular monolith | Accepted |
| 002 | PostgreSQL only system of record; no MongoDB | Accepted |
| 003 | Single-tenant | Accepted |
| 004 | Python, FastAPI, SQLAlchemy async + psycopg 3, Pydantic, Alembic | Accepted |
| 005 | React SPA with Vite | Accepted |
| 006 | Same-origin deployment | Accepted |
| 007 | Opaque server-side session tokens, not JWT | Accepted |
| 008 | Argon2id; MFA mandatory for every account; TOTP now, passkeys Phase 2 | Accepted (revised in review) |
| 009 | Procrastinate (PostgreSQL job queue) | Accepted |
| 010 | Redis: ephemeral rate-limit counters only | Accepted (revised in review) |
| 011 | Event-based attendance; clocking in separate from signing in | Accepted |
| 012 | Leave balances as a ledger | Accepted |
| 013 | Effective dating with exclusion constraints | Accepted |
| 014 | Application-layer authorization; no RLS in MVP | Accepted |
| 015 | Application-level encryption for sensitive identifiers only | Accepted |
| 016 | Audit in PostgreSQL: sealed chain, signed checkpoints, write-once anchors | Accepted (revised in review) |
| 017 | Minimal frontend libraries | Accepted |
| 018 | UUIDv7 keys | Accepted |
| 019 | Private object storage, uploads through the API | Accepted |
| 020 | OpenAPI-generated frontend types | Accepted |
| 021 | Private chat deferred to Future | Accepted (revisit only on Sigvitas request) |
| 022 | AI as a permission-bound tool layer; pgvector | Accepted (Future) |
| 023 | Hosting | **Pending Sigvitas input** |
| 024 | Migration ownership: Alembic applies application and Procrastinate schemas | Accepted (new in review) |
| 025 | Super admin least privilege and controlled elevation | Accepted (new in review) |
| 026 | HR policy values are configuration, never code | Accepted (new in review) |

---

## ADR-001 Modular monolith

- **Context.** Small team, one organization, tightly related HR domains, strict need for atomic audit writes.
- **Decision.** One FastAPI application with enforced module boundaries (`architecture.md` §4), plus a worker process from the same codebase.
- **Alternatives.** Microservices — rejected: distributed transactions for audit, operational overhead, no scaling need. Unstructured monolith — rejected: boundaries erode, extraction later becomes impossible.
- **Consequences.** Boundaries must be policed by import-linter in CI. Any module can be extracted later because it talks to others only through `public.py` and events.

## ADR-002 PostgreSQL as the only system of record; no MongoDB

- **Context.** MongoDB Atlas is available. HR data is relational (employees ↔ jobs ↔ departments ↔ approvals), needs constraints (non-overlapping effective dates, uniqueness), transactions spanning modules, and auditability.
- **Decision.** PostgreSQL 18 for all persistent data, including the job queue, full-text search and (later) vector embeddings via pgvector.
- **Alternatives.** MongoDB for documents/chat/audit — rejected: no concrete need that PostgreSQL does not meet (`jsonb`, partitioning); adds a second backup, security and consistency surface.
- **Consequences.** One database to secure, back up and restore. Revisit only for a measured need (e.g. chat volume far beyond expectations).

## ADR-003 Single-tenant

- **Context.** The system serves Sigvitas only.
- **Decision.** No `tenant_id`, no tenant routing.
- **Alternatives.** Multi-tenant from day one — rejected as speculative complexity touching every query and every authorization rule.
- **Consequences.** If Sigvitas ever needs separate legal entities, they are modelled as an `org.legal_entities` dimension, not tenants. Offering the product to other companies would require a dedicated project.

## ADR-004 Python + FastAPI + SQLAlchemy 2.0 (async, psycopg 3) + Pydantic v2 + Alembic

- **Context.** Preferred stack from the brief; needs typed validation, OpenAPI generation, mature ORM and migrations.
- **Decision.** As stated. psycopg 3 (not asyncpg) because it supports sync and async, is the PostgreSQL project's recommended driver, and is what Procrastinate uses — one driver.
- **Alternatives.** Django + DRF — strong built-in auth and admin, but the admin would be a second, harder-to-secure UI and its permission model is coarser than ours; FastAPI + Pydantic gives a cleaner API contract. NestJS/TypeScript backend — would unify languages, but the brief prefers Python and Python keeps AI work (later) close.
- **Consequences.** Auth, sessions and permissions are built in-house on well-reviewed primitives (argon2-cffi, pyotp or equivalent, `cryptography`). This is deliberate and gets the most review attention.

## ADR-005 React SPA with Vite, not a server-rendered framework

- **Context.** Authenticated internal tool; no SEO; strict CSP desired; mobile clients later share the API.
- **Decision.** React 19 + TypeScript + Vite, React Router in SPA (data/declarative) mode, static build served by the proxy.
- **Alternatives.** Next.js / React Router framework mode with SSR — adds a Node server tier that would also handle auth cookies and data fetching, doubling the security surface, for no user benefit here.
- **Consequences.** First load depends on JS; mitigated by route-level code splitting and a small shell budget.

## ADR-006 Same-origin deployment

- **Decision.** SPA and API served from one host (`/` and `/api`). No CORS configuration at all.
- **Consequences.** Simpler CSRF story (SameSite + custom header + Origin check); cookies can use the `__Host-` prefix. A mobile app uses bearer tokens and is unaffected.

## ADR-007 Opaque server-side session tokens, not JWT

- **Context.** Requirements include session revocation, device tracking, refresh rotation and immediate deactivation on exit.
- **Decision.** Random 256-bit access (15 min) and refresh (rotating, single-use with reuse detection) tokens, stored hashed in PostgreSQL, looked up on every request. Delivered as `HttpOnly` cookies to the web and in the body to mobile.
- **Alternatives.** Stateless JWT access tokens — revocation requires a denylist lookup anyway (losing the stateless benefit), and key management adds risk. Third-party identity provider (Keycloak, Auth0, Entra) — viable, but for one company it adds an external dependency; Phase 2 adds OIDC SSO *into* our session model if Sigvitas uses Google/Microsoft.
- **Consequences.** One indexed DB lookup per request for the session, plus permission resolution. There is no session or permission cache; adding one would need a new ADR (ADR-010).

## ADR-008 Argon2id passwords; MFA mandatory for every account; TOTP in MVP, passkeys in Phase 2

- **Context.** Every employee can see their own personal data and, from Phase 2, their payslips. A password-only employee account is a direct path to personal and salary data. The earlier draft left "MFA for everyone" as an open question; the review settled it.
- **Decision.** MFA is mandatory for **every** user account, with no opt-out setting and no password-only path. MVP: TOTP + 10 recovery codes. Phase 2: WebAuthn passkeys for everyone, required for `super_admin`, `system_admin` and `payroll_admin`. Accounts become active only after MFA enrolment. The last factor cannot be removed. Admin MFA reset requires password + emailed re-enrolment link. Step-up always re-verifies the MFA factor. Details in `security-architecture.md` §3.3.
- **Alternatives.** MFA only for privileged roles — rejected in review. SMS OTP — rejected (SIM swap, cost, deliverability). Email OTP as second factor — rejected (same channel as password reset).
- **Consequences.** Onboarding needs an authenticator app on each employee's phone. HR needs a short guide, and support must handle lost-device cases (recovery codes, admin reset). TOTP remains phishable until passkeys; accepted risk in `threat-model.md` §6.

## ADR-009 PostgreSQL-backed job queue (Procrastinate)

- **Context.** Need background jobs, scheduled jobs, and — importantly — jobs that are enqueued if and only if the business transaction commits (emails, recomputes, scans).
- **Decision.** Procrastinate (async, PostgreSQL `LISTEN/NOTIFY` + `SKIP LOCKED`, periodic tasks, retries), sharing the application's transaction.
- **Alternatives.** Celery + Redis — mature but heavy, and enqueueing to Redis cannot be atomic with a PostgreSQL commit (would require our own outbox). RQ/Dramatiq — same atomicity gap. arq — lightweight but Redis-bound (same atomicity gap) with limited active development.
- **Consequences.** Job throughput bounded by PostgreSQL, far above our needs (thousands of jobs/min). If Procrastinate stagnates, the outbox shape makes migration to another runner straightforward.
- **Schema ownership** is defined in ADR-024.

## ADR-010 Redis: ephemeral rate-limit counters only

- **Context.** With PostgreSQL holding data, jobs and lockout state, Redis must justify itself with a concrete MVP need or be removed.
- **Need.** The API runs as two or more replicas. Rate limits (per IP on auth endpoints, per user on the API, downloads and exports) must be counted across replicas. These counters are written on almost every request and are worthless after minutes. Putting them in PostgreSQL would add constant write load and WAL volume to the system of record, and put throwaway data into its backups.
- **Decision.** Keep Redis in the MVP for **shared rate-limit and throttling counters only**, with the exact key list, TTLs, configuration and failure behaviour in `architecture.md` §3.1. Redis is never authoritative for employees, attendance, leave, payroll, permissions, sessions, audit, security events, lockout state, documents, idempotency or jobs. There is no application cache in the MVP. WebSocket fan-out (Phase 2) evaluates PostgreSQL `LISTEN/NOTIFY` first.
- **Failure behaviour.** Auth, download and export limits fail closed (`503`). General API limits fail open with an alert. Account lockout (PostgreSQL) and proxy limits are unaffected.
- **Alternatives.** Remove Redis and count in PostgreSQL (unlogged table) — viable at our size, but mixes high-churn throwaway writes into the primary and makes rate limiting compete with business transactions. Rejected for now; it remains the fallback if Redis becomes an operational burden. Per-replica in-memory limits — rejected because limits would scale with replica count and reset on deploy.
- **Consequences.** One more private-network service, with no backups needed. Redis can be replaced with Valkey, or restarted, with no migration.

## ADR-011 Event-based attendance; clocking in is separate from signing in

- **Context.** The brief suggests `LOGIN/BREAK_START/BREAK_END/LOGOUT` events.
- **Decision.** Events are named `CLOCK_IN`, `BREAK_START`, `BREAK_END`, `CLOCK_OUT` and are explicit user actions. Signing in to the HRMS does not clock the user in. Events are append-only; corrections add events and void (never delete) originals; days are a recomputable projection.
- **Why.** Employees sign in to check payslips at home, on weekends, or on leave; an HR person may sign in many times a day. Tying attendance to authentication would record false attendance and make session timeouts affect pay. Keeping authentication events (`security_events`) separate from attendance also keeps the security log free of HR semantics.
- **Consequences.** If Sigvitas wants sign-in to prompt "Clock in now?", that is a UI nudge that needs no model change. All attendance *rules* (grace, thresholds, overtime, weekly offs) are Sigvitas policy values per ADR-026.

## ADR-012 Leave balances as a ledger

- **Decision.** `leave.leave_ledger` append-only entries; balance is a sum. No mutable balance column.
- **Alternatives.** Mutable balance with history table — prone to drift and hard to explain.
- **Consequences.** Every balance is explainable line by line to the employee. Aggregation cost is trivial at our volume; a cached balance view can be added if needed.

## ADR-013 Effective dating with database exclusion constraints

- **Decision.** Job assignments, shift assignments, compensation, payroll periods, attendance locks and active leave requests use date ranges with `EXCLUDE USING gist` to prevent overlaps at the database level.
- **Consequences.** Requires `btree_gist`. Changes are "close current row + insert new row" operations in services.

## ADR-014 Authorization in the application layer, no PostgreSQL RLS in MVP

- **Context.** Scopes depend on reporting-tree relationships, role-assignment constraints, SoD and step-up — logic that is awkward in RLS policies and hard to test there.
- **Decision.** Central authz engine in `platform/authz` producing both decisions and SQL scope filters; repositories require the filter. Database grants protect `audit` (append-only) and schema separation isolates `payroll`.
- **Alternatives.** RLS with session variables — revisit for `payroll` in Phase 2 as defence in depth if the team can maintain it.
- **Consequences.** Correctness rests on the engine and its tests; hence the route-coverage and matrix tests are mandatory CI gates.

## ADR-015 Application-level encryption for sensitive identifiers only

- **Decision.** AES-256-GCM envelope encryption for government IDs, bank accounts and TOTP secrets, with blind indexes for uniqueness. Compensation is protected by permissions, audit, schema grants and backup encryption, not column encryption.
- **Why.** Identifiers are never computed on, so encrypting them costs little. Compensation must be summed and compared in payroll; encrypting it would push computation into the application and complicate reporting for limited gain against the realistic threats (`threat-model.md` T3, T9).

## ADR-016 Audit in PostgreSQL: sealed chain, signed checkpoints, write-once anchors

- **Context.** Audit rows are partitioned and old partitions are eventually deleted by retention. An integrity scheme must survive retention, work across partition boundaries, detect missing partitions, and resist someone who can rewrite the database.
- **Decision.** Rows are written in the business transaction (same database, `audit` schema, INSERT/SELECT-only grants, append-only triggers). A single sealer job chains them per partition in an insert-only `chain_links` table. A continuous, signed checkpoint chain links partitions and days, and each checkpoint is anchored in write-once object storage. Retention removes a partition only after a signed tombstone checkpoint. Verification runs daily (recent) and monthly (full). Sensitive values are referenced, not copied. Full design in `security-architecture.md` §8.1.
- **Why chaining per partition plus checkpoints.** A single chain across all rows would break when a partition is dropped. Independent per-partition chains alone could not detect a dropped partition. The checkpoint chain gives both: partitions can be removed legitimately (tombstone), and illegitimate removal is detectable.
- **Why asynchronous sealing.** Computing the chain inside business transactions would serialize every audited write behind one lock. Sealing after commit keeps writes concurrent, at the cost of a sealing window of about a minute (accepted risk).
- **Alternatives.** External log service only — cannot be transactional with the change. Separate audit database — adds a second commit and failure mode. In-row `prev_hash` columns — need either serialized inserts or updates to immutable rows; rejected.
- **Consequences.** Requires a worker-only Ed25519 signing key, a write-once bucket, and `transaction_timeout` on application roles so month partitions can be closed deterministically.

## ADR-017 Frontend libraries: minimal and deliberate

| Concern | Choice | Why / alternatives |
|---|---|---|
| Styling | Tailwind CSS v4 | Token-driven via `@theme`; static CSS works with strict CSP. |
| Accessible primitives | React Aria Components | Strongest accessibility coverage including date/time pickers and tables, which HR screens need heavily; actively maintained by Adobe. Radix Primitives considered (smaller date support); shadcn/ui is a copy-in style kit on Radix that tends toward the generic look we want to avoid. |
| Server state | TanStack Query | Caching, retries, invalidation. |
| Tables | TanStack Table (headless) | Sorting/column logic without imposing markup. |
| Forms | React Hook Form + Zod | Per brief. |
| Routing | React Router | Per brief. |
| Icons | Lucide | Consistent stroke icons, tree-shakeable, ISC licence. |
| Animation | CSS first; Motion only where needed | Motion is the renamed Framer Motion. |
| Rich text sanitizing | DOMPurify | One component only. |
| Dates | `Intl` + `@internationalized/date` (ships with React Aria) | Avoids a second date library. |

Not included: global state libraries, CSS-in-JS, chart libraries (added only when a report genuinely needs a chart), UI kits.

## ADR-018 UUIDv7 primary keys

- **Decision.** `uuidv7()` defaults (native in PostgreSQL 18). Human-facing codes are separate.
- **Consequences.** IDs leak creation time roughly; acceptable since authorization, not ID secrecy, protects resources.

## ADR-019 Private object storage via S3-compatible API; uploads through the API

- **Decision.** Private buckets, random keys, uploads streamed through the API into quarantine, ClamAV scan in the worker, 60-second presigned GET URLs for downloads.
- **Alternatives.** Presigned PUT direct uploads — faster for large files but bypass server-side type checks until after upload; our files are small (≤ 20 MB).
- **Consequences.** The local storage emulator is chosen at M1; MinIO's community distribution model changed in 2025, so its current licensing/distribution will be checked against alternatives (e.g. SeaweedFS, Garage) at that time. Production uses the hosting provider's object store.

## ADR-020 API contract generated from FastAPI, types generated for the frontend

- **Decision.** OpenAPI committed and checked in CI; `openapi-typescript` for TS types.
- **Consequences.** Backend model changes surface as frontend type errors immediately.

## ADR-021 Private chat deferred to Future

- **Decision.** Not built in MVP or Phase 2 unless Sigvitas confirms a need and approves an access and retention policy.
- **Why.** Highest privacy risk, least coupling to HR workflows, likely duplicate of an existing company tool. The data model and access rules are designed (`database-design.md` §4.12, `authorization-model.md` §10) so it can be added cleanly.

## ADR-022 AI as a permission-bound tool layer; pgvector for retrieval

- **Decision.** Per `architecture.md` §9. No LLM gets database access; tools call services as the user; restricted data never embedded; retrieval filtered before ranking.
- **Alternatives.** Text-to-SQL over the HR database — rejected: cannot be made authorization-safe. Separate vector database — rejected until pgvector is shown insufficient.

## ADR-023 Hosting (pending)

- **Decision pending.** Requirements: managed PostgreSQL 18 with PITR, private object storage with public-access block, secrets manager, container runtime, region near users (India region if Sigvitas operates in India). Options to evaluate with Sigvitas: AWS (RDS, S3, ECS/App Runner), Azure (if Microsoft 365 shop), GCP (Cloud SQL, GCS, Cloud Run), or a single well-managed VM with managed database for lowest cost.

## ADR-024 Migration ownership: Alembic applies both application and Procrastinate schemas

- **Context.** Two things change the database: our application and the Procrastinate library. Two uncoordinated runners would produce ambiguous ordering and deployments that are hard to reproduce.
- **Decision.** Alembic is the single runner. The backend team owns application revisions. Procrastinate owns the *content* of its schema. We apply its release SQL files, unchanged and checksum-verified, through Alembic revisions. `procrastinate schema --apply` is never run against staging or production. Every revision follows expand/contract so code rollback never needs schema rollback. Production and staging are forward-only. Migrations run as a one-off job, as `hrms_migrator`, before the worker and API roll out. Details in `database-design.md` §11.
- **Alternatives.** Let Procrastinate manage its own schema — two sources of truth for ordering. A custom migration tool — unnecessary.
- **Consequences.** Upgrading Procrastinate is a deliberate PR with vendored SQL. CI checks: single Alembic head, checksums of vendored files, grants and catalog after upgrade, and the up/down/up cycle for application revisions.

## ADR-025 Super admin least privilege and controlled elevation

- **Context.** The first draft described super admin as "nothing is hidden", which contradicts least privilege and makes the super admin account the single most valuable target.
- **Decision.** `super_admin` manages roles and permissions, runs the elevation workflow, performs break-glass recovery, and reads audit and security events. It has **no** standing access to salary, payroll, personal or sensitive employee data, documents or messages. Data roles come only through a time-bound elevation approved by a different super admin, with step-up on both sides, audit, and notification to all super admins. Break-glass self-activation is limited to `system_admin` for 1 hour. Super admins cannot hold standing data roles. Assigning `super_admin` needs a second super admin. Details in `authorization-model.md` §4.2–4.3 and §6.
- **Alternatives.** An unrestricted super admin with after-the-fact audit only — rejected: detection without prevention for the most sensitive data. No super admin at all (only fixed roles) — rejected: someone must be able to recover access.
- **Consequences.** Sigvitas needs at least two super admins, chosen from people who do not need HR or payroll data daily. Occasional elevation requests add friction by design.

## ADR-026 HR policy values are configuration, never code

- **Context.** Sigvitas has not yet supplied its attendance and leave rules, and HR rules change over time. Hard-coding common practice (for example, a fixed grace period, "Saturdays off", a financial-year leave cycle, or Indian identifiers) would be wrong for Sigvitas or would need code changes later.
- **Decision.** The architecture defines mechanisms. Shifts, attendance policies, leave types, leave policies (accrual, carry-forward, lapse, negative balance, leave year), identifier types, document categories, holiday calendars, retention periods, locale and currency are effective-dated or typed configuration records. Domain logic consists of pure functions over those records. No production values are seeded. Computation for a location is blocked, with a visible setup gap, until its policies exist. `product-requirements.md` §6.12 is the authoritative list of parameters.
- **Consequences.** Admin screens for policy configuration are part of the MVP (M3, M4). Policy changes are audited with old and new values and trigger recomputation from their effective date. A new *kind* of rule is a code change with an ADR; a new *value* never is.

---

# Industry Research and Our Decisions

Research was limited to publicly documented capabilities of established HRMS products (vendor sites and help centres). No UI was copied; the purpose is to learn which patterns are common and decide deliberately where we follow or differ. Sources are listed at the end.

## 1. Module coverage

- **Commonly done.** Keka, greytHR, Zoho People and Darwinbox all bundle core HR, attendance, leave, payroll and employee self-service, then extend into performance, recruitment, expenses and engagement. Darwinbox and Workday cover the full hire-to-retire lifecycle at enterprise depth.
- **What we will do.** Core HR, attendance, leave and documents in MVP; payroll/payslips first in Phase 2; performance, recruitment and expenses out of scope.
- **Why.** Sigvitas needs a dependable daily core more than breadth. Every added module multiplies the permission and audit surface.

## 2. Employee self-service

- **Commonly done.** ESS portals let employees apply for leave, track attendance, raise requests, view payslips and update profiles; a team leave calendar is common (Keka, greytHR). BambooHR gives HR per-field control over what employees can view, edit, or edit-with-approval.
- **What we will do.** Self-service for profile, personal details, attendance, leave, documents and (Phase 2) payslips. Field tiers instead of per-field configuration in MVP; "edit with approval" for selected fields in Phase 2.
- **Why.** Per-field configuration is flexible but easy to misconfigure. Tiers (`directory`, `internal`, `personal`, `sensitive`, `restricted`) are fewer decisions and map directly to permissions and tests; approval-gated edits add the useful part of BambooHR's model later.

## 3. Attendance

- **Commonly done.** Clock-in via web, mobile, GPS and biometric devices; policies applied automatically (late marks, overtime); regularization requests approved by managers; HR bulk regularization; separate request types for WFH, on-duty and partial days (Keka, greytHR).
- **What we will do.** Event-based clock-in/out and breaks with server time; derived day summaries; correction requests that preserve originals; HR-initiated corrections; locks aligned to payroll. Geo/IP policies Phase 2; biometric integration Future; WFH/on-duty as request types in Phase 2 if Sigvitas uses them.
- **Why.** Most products expose "regularization" as editing the day's record. We keep the original events and add corrections, so disputes can always be answered with what was recorded and what was changed, by whom.

## 4. Leave

- **Commonly done.** Configurable leave types, accrual, carry-forward, multi-stage approval workflows with escalation and skip rules, replacement approvers, shared team calendars (Keka).
- **What we will do.** Configurable types and effective-dated policies; ledger-based balances; manager approval in MVP; multi-level chains, delegation and escalation in Phase 2; team calendar that shows absence without reasons.
- **Why.** Ledger balances make every number explainable. Approval chains are valuable but add configuration complexity; a single manager step covers most small-to-mid organizations and the schema already supports multiple steps.

## 5. Payroll

- **Commonly done.** Payroll runs with input cutoffs, lock/release stages, payslip release per employee, and statutory computation (greytHR's payroll lock and selective payslip release; Indian vendors bundle PF/ESI/PT/TDS).
- **What we will do.** Phase 2: effective-dated compensation, configurable components, payroll periods `draft → in_review → finalized → published` with four-eyes finalization, immutable payslip snapshots, attendance locks tied to periods. No statutory computation until requested.
- **Why.** The lifecycle (prepare, review, lock, release) is the proven part and is independent of statutory rules; building it first lets statutory logic be added as components and calculators later.

## 6. Approval workflows

- **Commonly done.** Configurable multi-step workflows, email approval with action buttons, forwarding, delegation (greytHR, Keka).
- **What we will do.** In-app approvals with explicit endpoints per transition; notifications by email link to the app. No approve-from-email buttons.
- **Why.** Email action links are convenient but turn an email into a bearer credential for a sensitive action and bypass step-up and session checks. Signing in to approve is a small cost for an HRMS holding salary data.

## 7. Reporting

- **Commonly done.** Dashboards for executives and managers, leave and attendance trends, department statistics, exportable reports.
- **What we will do.** Operational reports (attendance, leave) in MVP with scoped data and audited, step-up-protected exports. Charts only where a trend is the answer; no metrics dashboard on the home page.
- **Why.** Reports are a classic side door to data. Making reports inherit data permissions removes that risk, and keeping the home screen task-focused matches how employees use the product.

## 8. Security and permissions

- **Commonly done.** BambooHR: access levels (full admin, manager, employee, custom) with field-level view/edit and population scoping (all employees vs direct reports). Workday: security groups (user-, role-, intersection-, segment-based) attached to domain and business-process policies, with constrained (organization-scoped) vs unconstrained groups.
- **What we will do.** Permission keys with `self/team/all` scopes (close to Workday's constrained vs unconstrained idea), role assignments constrained by department/location, derived manager role from reporting lines (similar in effect to Workday's role-based groups), field tiers for data classes, SoD rules and step-up.
- **Why.** Workday's model is powerful but requires dedicated security administrators; BambooHR's is simple but coarse for payroll separation. Our model sits between: few concepts, explicit scopes, and defaults that keep salary, sensitive data and security administration apart.

## 9. Mobile

- **Commonly done.** Mobile apps for clock-in, leave, approvals and payslips; Darwinbox emphasises mobile-first with a voice-capable assistant.
- **What we will do.** Mobile-first responsive web in MVP with the three core mobile tasks (clock, leave, approve) optimized; token model and API already mobile-ready; PWA in Phase 2; native apps only if needed.
- **Why.** A responsive web app covers the core tasks without app-store overhead; the API and session design keep the native option open.

## 10. AI capabilities

- **Commonly done.** Zoho People's Zia assistant (query resolution, analytics, stated to be role-based); Darwinbox advertises multiple embedded AI agents and an HCM MCP server; chatbots for employee queries are becoming standard.
- **What we will do.** Nothing in MVP. Future assistant uses a tool layer that runs as the requesting user, read-only first, with retrieval filtered by permissions and restricted data never embedded. An MCP interface, if added, reuses the same tool layer.
- **Why.** The pattern across vendors confirms demand for employee Q&A and HR summaries, but the only safe design is one where the assistant cannot see more than the user. Building the permission-bound service layer first means AI is an addition, not a rewrite.

## Sources

- Keka — Attendance management: https://www.keka.com/attendance-management-system
- Keka — Bulk attendance regularisation: https://help.keka.com/admin/admin-help/bulk-attendance-regularisation
- Keka — Employee self-service portal: https://www.keka.com/employee-self-service-portal
- Keka — Tracking and managing your team's attendance: https://help.keka.com/hc/en-us/articles/39946857859985-Tracking-and-managing-your-team-s-attendance
- greytHR — Lock payroll and release payslips for selected employees: https://admin-help.greythr.com/admin/answers/122119665/
- greytHR — Employee self-service portal: https://www.greythr.com/employee-self-service-portal/
- greytHR — Approve/reject leave and attendance regularization: https://www.greythr.com/videos/how-to-admin/attendance-regularization-requests/
- BambooHR — Beginner's guide to access levels: https://learn.bamboohr.com/beginners-guide-to-access-levels
- BambooHR — Access levels in HR software: https://www.bamboohr.com/blog/access-levels-bamboohr
- Workday — Security group configuration and constraints: https://doc.workday.com/workday-education/en-us/course-manuals/security-for-administrators/security-group-configuration-and-constraints.html
- Workday — Configurable security framework: https://doc.workday.com/workday-education/en-us/course-manuals/hcm-core-for-administrators/configurable-security-framework.html?toc=8
- Zoho People — What's new Q2 2026: https://help.zoho.com/portal/en/community/topic/zoho-people-whats-new-q2-2026
- Darwinbox vs Zoho People comparison (secondary source): https://hrone.cloud/blog/darwinbox-vs-zoho-people/
