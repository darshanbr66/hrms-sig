# Sigvitas HRMS — Development Roadmap

Status: v0.2, after the Phase 0 architecture review and correction pass (2026-09-30).
Sizing is relative (S/M/L/XL) until team size and availability are known.

## Phase 0 — Architecture and planning (complete, pending the inputs in §6)

Deliverables: this documentation set. The architecture is internally consistent and approved for Phase 1. Open questions in §6 are Sigvitas inputs. None of them blocks M1 engineering except hosting for the staging deployment step.

## MVP

The MVP is built as a sequence of milestones. Each milestone ends in a working, tested, deployable increment on staging — never a half-built module.

### M1 — Foundation ("Phase 1") · L

Nothing user-facing beyond sign-in, MFA and the app shell, but everything else depends on it being right.

1. **Repository and CI:** lint, types, tests, import boundaries, secret scan, dependency audit, OpenAPI contract check, single-Alembic-head check, vendored Procrastinate SQL checksum check, banned-words check.
2. **Local environment** via Docker Compose: PostgreSQL 18, Redis (configured per `architecture.md` §3.1), S3-compatible storage emulator (including an object-lock bucket for audit anchors), ClamAV, mail catcher.
3. **Backend platform:** typed settings, DB session/unit of work, RFC 9457 errors, structured logging with redaction, clock abstraction, Procrastinate wiring, health endpoints, security headers, Redis rate limiter with the defined fail-closed/fail-open behaviour.
4. **Migrations** (`database-design.md` §11): Alembic setup; revision applying the pinned Procrastinate base schema; database roles, grants, default privileges, `statement_timeout`/`transaction_timeout`; migration job as a one-off task; expand/contract migration notes in the commit message (`engineering-principles.md` §8).
5. **Identity:** users, invites with password + mandatory TOTP enrolment + recovery codes, login (always two steps), enrolment-only sessions, sessions with access/refresh rotation and reuse detection, step-up (TOTP only), password reset (keeps MFA), admin MFA reset with re-enrolment link, own sessions and login history, PostgreSQL account lockout plus Redis per-IP throttling.
6. **Access:** permission catalog in code (synced by data migration), system roles, role assignments with constraints and validity windows, derived roles, per-request permission resolution, authz engine (`require`, `scope_filter`, SoD rules 1–10, step-up), role grant requests (elevation, super admin assignment, break-glass, acknowledgement), route-coverage test, authorization matrix test harness.
7. **Audit:** `audit_log` and `security_events` partitioned on `recorded_at`, append-only grants and triggers, sealer job, `chain_links`, signed checkpoints (daily and partition-final), write-once anchoring, daily and monthly verification, retention job with tombstones (exercised against test partitions), tamper test suite.
8. Minimal `people.employees` + `org` tables needed to link a user to an employee and resolve teams (full HR screens are M2).
9. **Frontend:** Vite + React + TS, design tokens, core design-system components (Button, fields, Dialog, AlertDialog, Toast, Table shell, EmptyState/ErrorState, Skeleton), app shell with responsive navigation, invite activation with MFA enrolment, sign-in, recovery-code sign-in, step-up dialog, sessions and MFA factors pages, a minimal super admin screen for role assignment and grant requests.
10. **Bootstrap CLI:** invite the first two super admins; refuses to run if any super admin exists.
11. **Staging deployment** with TLS, secrets management, object-lock anchor bucket, migration job in the pipeline, backups and a verified restore.

**Exit:** two super admins bootstrap; one invites a user; the user activates only after enrolling MFA; signs in on desktop and mobile; sees and revokes sessions. An elevation request is approved by the other super admin and expires on time. Break-glass works only for `system_admin`. The authorization matrix, SoD, MFA-invariant and audit tamper tests pass. A staging deployment runs migrations through the pipeline. The staging restore test passes.

### M2 — Organization and people · M

Locations (with time zones), departments, designations; employee create/edit; effective-dated job history and reporting lines; personal tier; identifier-type catalogue, employee identifiers and bank accounts with encryption and masking; status changes including exit with immediate access revocation; directory with search; self-service profile; HR and role administration screens.

**Exit:** HR can set up Sigvitas's real structure and employee records; managers see exactly their team; exit revokes access.

### M3 — Attendance, breaks, shifts, corrections · L

Shift and attendance-policy configuration screens (effective-dated, versioned, with preview against recorded days) and the setup-gap view; shift assignments; clock in/out and breaks with server time and idempotency; `derive_day` as a pure, policy-parameterized function; day projection and recompute jobs (including recompute-from-effective-date on policy change); nightly materialization and incomplete detection; today view; self and team attendance views; correction request/approval with side-by-side timelines; HR-initiated corrections; attendance locks.

**Exit:** with Sigvitas's configured shifts and policies, a full month of attendance for all employees matches hand-checked cases, including midnight-crossing shifts and missing clock-outs. The same test suite also passes against deliberately different test policies, which proves that no value is hard-coded.

### M4 — Leave and holidays · L

Leave type and leave policy configuration screens (leave year, accrual, carry-forward, lapse, negative balance, half-day, attachment, day-counting rules); ledger with opening-balance import, accruals, adjustments, carry-forward, carry-forward expiry and lapse jobs; request with preview, overlap constraint and configured day counting; manager approval; cancellation flows; team leave calendar; holiday calendars per location; integration with attendance day status.

**Exit:** Sigvitas's opening balances imported for all employees; a year-end rollover dry run using Sigvitas's configured policies produces results HR signs off.

### M5 — Documents, notifications, reports · M

Document categories and sensitivity; upload with quarantine and scan; versioning; signed-URL download with audit; notifications (in-app + email outbox) with preferences; attendance and leave reports; export jobs with step-up, expiry and formula-injection protection; audit log, security events and elevation history viewers.

### M6 — Hardening and launch · M

Performance test at design load; accessibility audit; OWASP ZAP; external penetration test and fixes; runbooks (restore, key rotation including the audit signing key, compromised account, break-glass, total super admin lock-out); MFA onboarding guide for employees; data import of real employees (by Sigvitas HR, not by engineering from spreadsheets on laptops); pilot with one team; production launch.

## Phase 2

Ordered by expected value; to be re-prioritized with Sigvitas after MVP launch.

1. **Compensation and payslips** — components, effective-dated compensation, payroll periods with four-eyes finalization, payslip snapshots and PDFs, secure self-service payslip access. (If Sigvitas uses an external payroll provider, an import + publish flow for payslips first.)
2. **Passkeys (WebAuthn)** — available to every user; required for super_admin, system_admin and payroll_admin.
3. **SSO via OIDC** (Google Workspace or Microsoft Entra ID, whichever Sigvitas uses), feeding our own session model. HRMS MFA still applies unless the identity provider's MFA is verified through the OIDC claims, which needs an ADR.
4. **Employee requests** — configurable request types, assignment, SLA, comments.
5. **Announcements** — targeted by department/location, sanitized rich text, read tracking.
6. **Onboarding and offboarding checklists** — templates, tasks with assignees, asset recovery.
7. **Assets** — inventory and assignments.
8. **Profile change approvals** — selected self-service edits require HR approval.
9. **Multi-level approval chains** for leave and corrections; approval delegation during absence.
10. **Global search** across modules using PostgreSQL full-text search with scope filters.
11. **Custom roles UI** with guardrails.
12. **Attendance location controls** — IP ranges / geofence per location. The rule engine for thresholds, breaks and overtime is already MVP; this adds *where* clock-in is allowed.
13. **PWA** install and web push notifications; real-time updates (WebSocket or SSE; fan-out mechanism decided then per ADR-010).
14. **Access review report** and anomaly alerts (download/export volumes).
15. **Peer "away" calendar** (`leave.calendar.read.department`), if Sigvitas wants it.

## Future

- **Private messaging** — only if Sigvitas decides HR conversations should move into this system and an access/retention policy is approved.
- **AI assistant** — self-service Q&A over company policies, "what's my balance" style queries, HR summaries; built on the tool-adapter design in `architecture.md` §9 with pgvector.
- **Statutory payroll** for Sigvitas's jurisdiction (for India: PF, ESI, PT, TDS), if Sigvitas wants payroll in-house.
- **Native mobile apps** — if the responsive web app/PWA proves insufficient (e.g. background location, biometric unlock).
- **Biometric/attendance device integration.**
- **Performance management, recruitment, expenses** — out of current scope.

## Risks to the plan

| Risk | Mitigation |
|---|---|
| Scope creep into Phase 2 during MVP | Milestone exit criteria; Phase 2 list is the parking lot |
| Sigvitas attendance and leave policies arrive late or need a rule *kind* the engine lacks | Values are configuration (ADR-026), so late values do not block engineering; policy parameters (`product-requirements.md` §6.12) are reviewed with HR before M3/M4 starts, so any missing rule kind is found early |
| Mandatory MFA friction at rollout | Guided enrolment in the invite flow; employee guide; recovery codes; clear admin reset procedure; pilot team first |
| Audit integrity design complexity | Built and tamper-tested in M1, before any business module depends on it |
| Data import quality | Import tooling with validation report; HR owns corrections |
| Security review finds structural issues late | M1 builds the security core first; external test booked early for M6 |
| Single-engineer bus factor on auth/payroll code | Second-engineer review before push, recorded in the commit; when none is available, an owner-authorized adversarial self-review with the human review still owed (`engineering-principles.md` §8); runbooks |

## 6. Decisions needing Sigvitas input

Engineering decisions are final (see the ADR status index). The items below are Sigvitas inputs. Each is needed by the milestone shown, not before.

| Input | Needed by |
|---|---|
| Hosting provider and region; transactional email provider (ADR-023) | M1 step 11 (staging) |
| Names of the first two super admins, chosen among people who do not need HR or payroll data daily (ADR-025); whether to appoint an auditor | M1 bootstrap |
| Google Workspace or Microsoft 365 (future SSO) | Phase 2 (no M1 impact) |
| Approximate headcount, number of locations and time zones | M1 sizing check, M3 |
| Brand colours and logo (typeface is IBM Plex Sans unless Sigvitas has a brand font) | M1 design tokens |
| Confirmation of the default role grants in `authorization-model.md` §4.1 (the least-privilege boundaries themselves are fixed) | M2 |
| Identifier types HR must collect, and bank details needed (minimum necessary) | M2 |
| Attendance policy values: shifts, weekly offs, grace, late/early rules, full/half-day thresholds, break rules, overtime, incomplete-day rule, multiple sessions (`product-requirements.md` §6.12) | M3 |
| Whether signing in should *prompt* "Clock in now?" (UI nudge only; ADR-011) | M3 |
| Leave policy values: leave types, leave year start, accrual, opening balances, carry-forward, lapse, negative balance, half-day, attachments, day counting | M4 |
| Document categories, sensitivity and retention | M5 |
| Legal retention periods for HR, payroll and audit data | M5 (retention jobs), M6 |
| Default locale, currency and week start (en-IN / INR proposed) | M1 settings (changeable later) |
| Whether accounts may be invited before the joining date, and what a pre-joining account may see (the derived `employee` role applies from the joining date) | M2 |
| Whether `user.disable` may target administrator accounts (`system_admin`, `super_admin`), and by whom; today any holder may disable any other account | Checkpoint E |
| Rehire: a new employee record or the same one (one `employees` row holds one joining and exit date) | M2 |
| Payroll: in-house calculation or external provider; four-eyes on by default | Phase 2 |
| Chat: needed at all; investigation-access policy | Future |
| DPDP Act obligations and owners (privacy notice, grievance officer, breach process), if applicable | M6 launch |
