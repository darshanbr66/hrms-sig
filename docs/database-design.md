# Sigvitas HRMS — Database Design

Status: Draft v0.1. This is the logical design; column types and names will be finalized in migrations during each phase.
Engine: PostgreSQL 18.

## 1. Conventions

| Topic | Rule |
|---|---|
| Primary keys | `id uuid PRIMARY KEY DEFAULT uuidv7()` (native in PostgreSQL 18). Time-ordered, index-friendly, not guessable in sequence. Human-facing identifiers (employee code) are separate columns. |
| Timestamps | `timestamptz`, stored UTC. Every mutable table has `created_at`, `updated_at`; most also `created_by`, `updated_by` (user id). |
| Dates | `date` for calendar concepts (work date, leave dates, effective dates). |
| Effective dating | `effective_from date NOT NULL`, `effective_to date NULL` (exclusive). Non-overlap enforced with `EXCLUDE USING gist (employee_id WITH =, daterange(effective_from, effective_to) WITH &&)` (`btree_gist` extension). |
| Money | `numeric(14,2)` + `currency char(3)`. Never floating point. |
| Enumerations | `text` with `CHECK (col IN (...))` constraints, mirrored by Python `StrEnum`. Easier to evolve than PostgreSQL `ENUM` types. |
| Optimistic concurrency | `version integer NOT NULL DEFAULT 1` on user-editable aggregates; updates check and increment. |
| Soft deletion | Not a blanket rule. HR records are never deleted; they change status (`archived`, `exited`). `deleted_at` is used only for user content that users can remove (documents, notifications, future messages). Hard deletion happens only through retention jobs. |
| Naming | `snake_case`, plural table names, FK columns `<entity>_id`. Constraints named `pk_`, `fk_`, `uq_`, `ck_`, `ix_`, `ex_` + table + columns (SQLAlchemy naming convention). |
| Schemas | One PostgreSQL schema per module. Makes ownership visible and allows grants per schema (payroll, audit). |
| JSON | `jsonb` only for genuinely variable data (notification payload, settings values validated by Pydantic, audit change lists). Never for data that is queried relationally. |
| Text limits | `varchar(n)` or `CHECK (length(col) <= n)` on every free-text column. |

## 2. Schema map

| Schema | Tables | Module |
|---|---|---|
| `identity` | users, credentials, mfa_factors, recovery_codes, sessions, session_tokens, one_time_tokens, trusted_devices | identity |
| `access` | permissions, roles, role_permissions, user_roles, role_grant_requests | access |
| `org` | locations, departments, designations | org |
| `people` | employees, employee_jobs, employee_personal, identifier_types, employee_identifiers, employee_bank_accounts, emergency_contacts, employee_status_history | people |
| `attendance` | shifts, shift_versions, shift_assignments, attendance_policies, attendance_events, attendance_event_voids, attendance_days, attendance_corrections, attendance_correction_items, attendance_locks | attendance |
| `leave` | leave_types, leave_policies, leave_ledger, leave_requests, leave_request_days, leave_approvals, holiday_calendars, holidays, location_holiday_calendars | leave |
| `payroll` | salary_components, compensations, compensation_lines, payroll_periods, payroll_entries, payroll_entry_lines, payslips | payroll (Phase 2) |
| `docs` | document_categories, documents, document_versions | documents |
| `notify` | notifications, notification_preferences, email_outbox, announcements (Phase 2), announcement_audiences (Phase 2) | notifications |
| `audit` | audit_log (partitioned), security_events (partitioned), chain_links (partitioned), chain_checkpoints | audit |
| `app` | settings, idempotency_keys, export_jobs | settings / platform |
| Procrastinate objects | job queue tables, functions and types defined by the pinned Procrastinate release. Applied through Alembic (§11), never edited by us. | platform |
| Phase 2/Future | `requests`, `assets`, `lifecycle`, `chat`, `ai` | |

## 3. Database roles and grants

| Role | Grants |
|---|---|
| `hrms_migrator` | Owner of all schemas and of the Procrastinate objects. Used only by the migration job (§11). Sets default privileges so new tables get the grants below. |
| `hrms_app` | `SELECT, INSERT, UPDATE, DELETE` on module schemas, except `SELECT` only on the permission catalog (`access.permissions`, `access.roles`, `access.role_permissions`); on `audit.audit_log` and `audit.security_events`: `SELECT, INSERT` only; on `audit.chain_*`: `SELECT` only; on `payroll`: full DML (application enforces permissions); DML and function execution on Procrastinate objects (to enqueue jobs). `statement_timeout = 5s`, `transaction_timeout = 30s`, `idle_in_transaction_session_timeout = 10s`. |
| `hrms_worker` | Same as `hrms_app`, plus `INSERT` on `audit.chain_links` and `audit.chain_checkpoints`, and `EXECUTE` on `audit.ensure_partitions` (§4.10). `statement_timeout = 5min`, `transaction_timeout = 10min`. Only the worker has the checkpoint signing key (outside the database). |
| `hrms_audit_retention` | `SELECT` on the `audit` schema (to verify before removal); can detach and drop expired partitions of `audit.audit_log`, `audit.security_events` and `audit.chain_links`; `INSERT` on `audit.chain_checkpoints` (tombstones), with no update or delete. Used only by the retention job, which runs in the worker process (where the signing key lives) on a separate connection (`security-architecture.md` §8.1). `statement_timeout = 5min`, `transaction_timeout = 10min` (the worker's limits). |
| `hrms_readonly` (optional) | `SELECT` on non-sensitive schemas for operational reporting; no access to `payroll`, `audit`, `identity`, `people.employee_identifiers`, `people.employee_bank_accounts`, `people.employee_personal`. |

The one-time cluster bootstrap (`infra/db/bootstrap-roles.sql`, run by a database administrator) creates these login roles and the database owned by `hrms_migrator`, and gives `hrms_migrator` `CREATEROLE` plus `ADMIN OPTION` (without `INHERIT` or `SET`) on the runtime roles, so migrations can set their per-database session limits without acting as them. Everything else (schemas, grants, default privileges, limits) is applied by Alembic. `PUBLIC` has no privileges on the database (no `TEMP`) and cannot execute functions the migrator creates.

The `transaction_timeout` values bound how long a transaction can stay open. The audit sealer relies on that bound to close a month partition safely.

## 4. Tables

Only key columns and constraints are listed. All tables have `id`, and timestamp columns per §1 unless stated.

### 4.1 identity

**users** — a login identity.
- `email citext NOT NULL UNIQUE`, `email_verified_at`, `status` (`invited`, `active`, `disabled`), `employee_id uuid NULL UNIQUE → people.employees` (1:0..1; null for accounts with no employee record, such as the bootstrapped super admins), `last_login_at`, `failed_login_count smallint`, `failed_login_window_started_at`, `last_failed_login_at` (for the progressive delay), `locked_until timestamptz NULL`, `version`. `CHECK (status <> 'active' OR email_verified_at IS NOT NULL)`: activation verifies the email.
- `mfa_reenrolment_required boolean NOT NULL DEFAULT false` (set by an admin MFA reset; restricts the account to enrolment endpoints) arrives with the MFA reset flow (Checkpoint E).
- A user is linked to at most one employee and vice versa.
- There is no "MFA required" flag, because MFA is required for everyone. Invariant: an `active` account has at least one confirmed, unrevoked MFA factor. It is enforced in the database by a deferred constraint trigger (`identity.require_mfa_for_active_users`) on `users` (status) and `mfa_factors` (confirmation, revocation, deletion), which locks the account row before checking, so concurrent removals of different factors cannot both commit. The service also refuses to remove the last factor (`409 mfa.last_factor`). E extends the trigger for `mfa_reenrolment_required`.

**credentials** — `user_id UNIQUE → users`, `password_hash text` (Argon2id encoded), `password_changed_at`. Separate table so user queries never load the hash.

**mfa_factors** — `user_id → users`, `type` (`totp`; `webauthn` with its `webauthn_credential_id` and `public_key` in Phase 2), `label`, `secret_ciphertext bytea` (AES-256-GCM, associated data bound to the purpose and the user), `secret_key_version smallint` (key rotation), `last_used_step bigint` and `last_used_at` (TOTP replay prevention: a step is accepted only if later than the last, in one atomic UPDATE), `confirmed_at`, `revoked_at`. Index `(user_id) WHERE revoked_at IS NULL`. A user may hold several factors (for example, a new authenticator enrolled before the old one is removed). The service caps active factors at 5; an unconfirmed factor expires after 15 minutes and is replaced by the next setup.

**recovery_codes** — `user_id`, `code_hash` (SHA-256 of an 80-bit code), `used_at`, `revoked_at` (set when the codes are regenerated). `UNIQUE (user_id, code_hash)`.

**sessions** — `user_id`, `client_type` (`web`; `mobile` with the mobile client), `scope` (`full`, `mfa_enrolment`), `ip inet`, `user_agent`, `created_at`, `last_activity_at`, `idle_expires_at`, `absolute_expires_at`, `step_up_at`, `revoked_at`, `revoked_reason` (`logout`, `revoked_by_user`, `revoked_by_admin`, `password_changed`, `password_reset`, `account_disabled`, `token_reuse`). Each session keeps the idle window it was created with, so a change to the idle-timeout setting applies to new sessions. `CHECK (idle_expires_at <= absolute_expires_at)`. Index `(user_id) WHERE revoked_at IS NULL`. New-device notifications use `trusted_devices`, not a session column.

**session_tokens** — `session_id → sessions ON DELETE CASCADE`, `kind` (`access`, `refresh`), `token_hash bytea UNIQUE` (SHA-256 of a 256-bit token), `expires_at`, `used_at` (refresh only), `replaced_by_id` (refresh chain). Lookups by `token_hash` only. Rotation ends the previous access tokens (`expires_at` set to now) and retires unused refresh tokens (`used_at`); a role change ends only the access tokens, so the next refresh issues new ones.

**one_time_tokens** — `user_id`, `purpose`, `token_hash UNIQUE`, `expires_at`, `used_at`, `failed_attempts smallint`. Purposes in use: `invite` (72 h), `invite_enrolment` (30 min; binds the authenticator steps of an invite to the browser that set the password), `login_mfa` (5 min; the password step's single-use proof, at most 5 wrong codes), `password_reset` (30 min by default). Invite and reset tokens are created by the worker when it sends the email, so a usable token exists only in the email. Issuing a token of a purpose retires the user's earlier unused ones; a password change or reset also retires pending `login_mfa` tokens. `email_change` and `mfa_reenrolment` (with `payload jsonb`) arrive with their flows. Purged 30 days after expiry by the retention job.

**trusted_devices** — `user_id`, `token_hash bytea UNIQUE` (SHA-256 of the random device cookie; no fingerprinting), `user_agent`, `created_at`, `last_seen_at`, `expires_at`, `revoked_at`, `revoked_reason` (`revoked_by_user`, `revoked_by_admin`, `password_changed`, `password_reset`, `mfa_changed`, `account_disabled`). Index `(user_id) WHERE revoked_at IS NULL`. Used for new-device notifications only, never as an authentication factor (`security-architecture.md` §3.4). Revoked rows are kept.

Login attempts are recorded in `audit.security_events`. Account lockout state is in `users` (PostgreSQL, authoritative). Only per-IP throttling counters live in Redis, and they are ephemeral (`architecture.md` §3.1).

### 4.2 access

**permissions** — `key text UNIQUE` (e.g. `leave.request.approve.team`), `description`, `requires_step_up boolean`, `audit_reads boolean`, `phase`. Seeded from the code catalog by migration; the code catalog is the source of truth.

**roles** — `key text UNIQUE`, `name`, `description`, `is_system boolean`, `is_derived boolean` (`manager`, `employee`).

**role_permissions** — `role_id`, `permission_id`, PK `(role_id, permission_id)`.

**user_roles** — `user_id`, `role_id`, `department_id NULL`, `location_id NULL`, `valid_from timestamptz`, `valid_until timestamptz NULL`, `granted_by`, `grant_reason`, `revoked_at`, `revoked_by`. Rows are revoked, not deleted, so history of who had what access is preserved: a trigger refuses DELETE and TRUNCATE for every role and allows an UPDATE only of `revoked_at`/`revoked_by`, once. Unique partial index on `(user_id, role_id, coalesce(department_id), coalesce(location_id)) WHERE revoked_at IS NULL`. Derived roles are never stored here; the engine computes them, and a composite foreign key `(role_id, role_is_derived) → roles (id, is_derived)` with `role_is_derived = false` makes storing one impossible. `CHECK (granted_by IS NULL OR granted_by <> user_id)` enforces SOD-3 in the database; `granted_by IS NULL` only for the installation bootstrap. `grant_request_id NULL → role_grant_requests` arrives with the grant request workflow (Checkpoint E), when the check becomes `... OR grant_request_id IS NOT NULL` and the service restricts a self-grant with a request to kind `break_glass`.

`permissions`, `roles` and `role_permissions` are written only by migrations: the runtime roles hold `SELECT` on them and nothing else, so no application bug can widen a role. The code catalog (`platform/authz/catalog.py`, `roles.py`) is the source of truth; a test checks the database copy matches it.

**role_grant_requests** (Checkpoint E) — requests that need a second person or are emergency grants (`authorization-model.md` §4.3).
- `kind` (`elevation`, `super_admin_assignment`, `break_glass`), `subject_user_id` (who receives the role), `role_id`, `department_id NULL`, `location_id NULL`, `reason text NOT NULL`, `requested_duration interval NULL`, `requested_by`, `requested_at`, `status` (`pending`, `approved`, `rejected`, `expired`, `active`, `ended`, `revoked`), `decided_by NULL`, `decided_at`, `decision_note`, `starts_at`, `ends_at`, `acknowledged_by NULL`, `acknowledged_at` (break-glass post-incident review).
- `CHECK (decided_by IS NULL OR decided_by <> requested_by)`.
- `CHECK (kind <> 'break_glass' OR (role is system_admin AND ends_at - starts_at <= interval '1 hour'))`. The role check is by a fixed role ID and is also enforced in the service layer.
- `CHECK (kind <> 'elevation' OR requested_duration <= interval '8 hours')`.
- Index `(status) WHERE status IN ('pending','active')`.

### 4.3 org

**locations** — `code UNIQUE`, `name`, `time_zone text NOT NULL` (IANA, validated), `country_code char(2)`, `address`, `status` (`active`, `archived`), `allowed_ip_ranges cidr[]` (Phase 2 attendance policy).

**departments** — `code UNIQUE`, `name`, `parent_id NULL → departments` (hierarchy), `head_employee_id NULL → people.employees`, `status`. `CHECK (parent_id <> id)`; cycles prevented in service layer.

**designations** — `code UNIQUE`, `name`, `level int NULL`, `status`.

Codes are unique regardless of case (`citext`). `time_zone` is checked against IANA zone names by the service that writes locations. No departments, designations or locations are seeded in production. Sigvitas enters its own.

### 4.4 people

**employees** — the HR record.
- `employee_code text UNIQUE NOT NULL`, `legal_first_name`, `legal_last_name`, `preferred_name`, `work_email citext UNIQUE`, `work_phone`, `photo_document_id NULL`, `date_of_joining date NOT NULL`, `date_of_exit date NULL`, `status` (`pre_joining`, `active`, `on_notice`, `exited`), `version`.
- `CHECK (date_of_exit IS NULL OR date_of_exit >= date_of_joining)`; `CHECK (status <> 'exited' OR date_of_exit IS NOT NULL)`. `legal_last_name` is optional, for people with a single legal name.
- Directory-search indexes (on `status`, and trigram indexes on the name fields and `employee_code`, `pg_trgm`) are created with the M2 directory query that uses them.

**employee_jobs** — effective-dated job assignment.
- `employee_id`, `effective_from`, `effective_to`, `department_id`, `designation_id`, `location_id`, `manager_employee_id NULL → employees`, `employment_type` (`full_time`, `part_time`, `contract`, `intern`), `change_reason` (`joining`, `transfer`, `promotion`, `manager_change`, `correction`, `other`), `notes`.
- `department_id`, `designation_id` and `location_id` are required.
- `EXCLUDE` non-overlap per employee. `CHECK (manager_employee_id <> employee_id)`.
- Rows are never deleted. An update may change only `effective_to` (closing the row); a trigger rejects any other update, delete or truncate, for every role.
- Indexes: `(manager_employee_id) WHERE manager_employee_id IS NOT NULL` for team resolution; `(department_id)` and `(location_id)` for role assignments restricted to a department or location. Team resolution selects the rows that apply on a date (`effective_from <= d < effective_to`), not open-ended rows, because a future-dated change leaves the current row closed and the future row open (`authorization-model.md` §7). Cycle prevention (A manages B manages A) in service layer with a recursive check; team resolution also stops at a cycle.

**employee_personal** — 1:1 with employees (`employee_id PK/FK`): `date_of_birth`, `gender` (optional, only if Sigvitas needs it), `personal_email`, `personal_phone`, `current_address jsonb`, `permanent_address jsonb`, `version`. Separate table so `internal` queries never touch personal data.

**identifier_types** — catalogue of government or other identifiers HR decides to collect: `code UNIQUE`, `name`, `country_code NULL`, `validation_pattern NULL` (regex checked in the service), `store_mode` (`full_encrypted` or `last4_only`), `unique_per_org boolean` (duplicate detection via blind index), `status`. Nothing is seeded in production. Examples of what Sigvitas *might* configure, not assumptions: PAN, Aadhaar (likely `last4_only` unless legally required), passport.

**employee_identifiers** — `employee_id`, `identifier_type_id`, `value_ciphertext bytea NULL`, `last4 varchar(4)`, `blind_index bytea NULL`, `key_version`, `valid_until date NULL`, `version`. `UNIQUE (employee_id, identifier_type_id)`. Unique partial index on `(identifier_type_id, blind_index)` where the type is `unique_per_org`.

**employee_bank_accounts** — effective-dated salary account: `employee_id`, `account_holder_name`, `account_number_ciphertext`, `account_last4`, `routing_code` (bank routing identifier; format depends on country, e.g. IFSC in India), `bank_name`, `effective_from`, `effective_to`, `key_version`, `version`. `EXCLUDE` non-overlap per employee.

Both tables are the `sensitive` tier. Separate tables keep `internal` and `personal` queries away from them.

**emergency_contacts** — `employee_id`, `name`, `relationship`, `phone`, `priority smallint`.

**employee_status_history** — `employee_id`, `from_status`, `to_status`, `effective_date`, `reason`, `changed_by`. Append-only.

### 4.5 attendance

Attendance is event-sourced. `attendance_events` is the source of truth; `attendance_days` is a projection. A separate `breaks` table is unnecessary: breaks are `BREAK_START`/`BREAK_END` event pairs and their durations are part of the day projection.

**Architecture versus policy.** The tables below hold *mechanism* (what can be configured) and *values* (what Sigvitas configures). No production row is seeded. `product-requirements.md` §6.12 lists every parameter and its owner.

**shifts** — the shift's identity: `code UNIQUE`, `name`, `status`.

**shift_versions** — *when* work is expected, effective-dated: `shift_id`, `version_no`, `effective_from`, `effective_to`, `start_time time`, `end_time time`, `crosses_midnight boolean` (derived from the times), `weekly_off_rule jsonb` (validated schema: a weekday set plus optional week-of-month exceptions). `UNIQUE (shift_id, version_no)`; `EXCLUDE` non-overlap per shift. Recomputing a past day uses the version effective on that day.

**shift_assignments** — `employee_id`, `shift_id`, `effective_from`, `effective_to`. `EXCLUDE` non-overlap. A location default shift is a setting. If an employee has no assignment and the location has no default, the day is not computed and HR sees a setup gap (ATT-12).

**attendance_policies** — *how* a day is judged. Effective-dated, with scope resolution most-specific-first: shift → location → organization default.
- `scope_type` (`organization`, `location`, `shift`), `scope_id NULL`, `effective_from`, `effective_to`, `version`.
- `grace_minutes`, `early_departure_grace_minutes`, `duration_basis` (`effective`, `gross`), `full_day_min_minutes`, `half_day_min_minutes`, `late_marks_per_half_day NULL` (NULL = no conversion), `max_break_minutes NULL`, `breaks_count_as_work boolean`, `break_excess_action` (`flag`, `deduct`), `overtime_enabled boolean`, `overtime_basis` (`beyond_full_day`, `beyond_shift_end`), `overtime_min_minutes`, `overtime_requires_approval boolean`, `incomplete_after_minutes`, `allow_multiple_sessions boolean`.
- `CHECK (half_day_min_minutes <= full_day_min_minutes)`; all minute values `>= 0`.
- `EXCLUDE` non-overlap per `(scope_type, scope_id)`.
- Validated by a Pydantic model; every change is audited with old and new values (policies contain no personal data).

**attendance_events** — append-only.
- `employee_id`, `event_type` (`CLOCK_IN`, `BREAK_START`, `BREAK_END`, `CLOCK_OUT`), `occurred_at timestamptz` (server time for live events; proposed time for correction events), `work_date date` (assigned by the shift rule at write time), `source` (`web`, `mobile`, `correction`, `import`), `correction_id NULL → attendance_corrections`, `recorded_by` (user), `recorded_at timestamptz DEFAULT now()`, `client_reported_at NULL`, `ip inet`, `user_agent`, `geo point NULL` (only if a location policy requires it and the user consented), `idempotency_key`.
- `UNIQUE (employee_id, idempotency_key)`.
- Index `(employee_id, work_date, occurred_at)`.
- No `UPDATE`/`DELETE` by the application (enforced by a trigger that raises on update/delete, and by code review).

**attendance_event_voids** — `event_id UNIQUE → attendance_events`, `correction_id → attendance_corrections`, `voided_at`, `voided_by`. An event is effective if it has no void row.

**attendance_days** — projection, rebuildable.
- `employee_id`, `work_date`, `shift_id`, `shift_version`, `attendance_policy_id`, `policy_version`, `inputs_snapshot jsonb` (the shift times and policy values actually used, for explanation and audit), `first_in_at`, `last_out_at`, `gross_minutes`, `break_minutes`, `effective_minutes`, `late_minutes`, `early_leave_minutes`, `overtime_minutes`, `status` (`present`, `half_day`, `absent`, `on_leave`, `holiday`, `weekly_off`, `incomplete`, `pending_correction`), `anomalies text[]` (e.g. `break_exceeded`, `missing_break_end`), `calc_version int`, `computed_at`.
- `UNIQUE (employee_id, work_date)`. Index `(work_date, status)`.
- Days with no events are materialized by a nightly job for active employees so `absent`, `holiday`, `weekly_off` and `on_leave` appear in reports.

**attendance_corrections** — `employee_id`, `work_date`, `reason varchar(1000)`, `status` (`pending`, `approved`, `rejected`, `cancelled`), `requested_by`, `requested_at`, `decided_by`, `decided_at`, `decision_note`, `version`. Partial unique: one pending correction per `(employee_id, work_date)`.

**attendance_correction_items** — `correction_id`, `action` (`add`, `void`), `event_type` (for add), `proposed_at` (for add), `target_event_id` (for void). On approval: add items become `attendance_events` with `source='correction'`; void items become `attendance_event_voids`; the day is recomputed. Original events remain.

**attendance_locks** — `period_start date`, `period_end date`, `locked_by`, `locked_at`, `reason`. `EXCLUDE` non-overlap. Corrections within a locked range require `attendance.correction.approve.all`.

**Derivation (architecture, fixed).** `derive_day(events, shift_version, policy_version, calendar_context) -> DaySummary` is a pure function. Every threshold comes from its parameters; none is a constant in code.
- Pair effective (non-voided) events chronologically. A `BREAK_START` without `BREAK_END` before `CLOCK_OUT` closes at `CLOCK_OUT` and flags `missing_break_end`.
- Sessions: each `CLOCK_IN`…`CLOCK_OUT` pair. Several sessions per day are summed only if `policy.allow_multiple_sessions`; otherwise a second `CLOCK_IN` is rejected at write time. Gaps between sessions are neither work nor break.
- `gross = Σ session durations`; `break = Σ break pairs`; `effective = gross − break`, or `gross` if `policy.breaks_count_as_work`.
- `late = max(0, first_in − (shift.start + policy.grace_minutes))`.
- `early_departure = max(0, (shift.end − policy.early_departure_grace_minutes) − last_out)`.
- Day status from `policy.duration_basis` against `policy.full_day_min_minutes` / `half_day_min_minutes`.
- Overtime only if `policy.overtime_enabled`, measured per `policy.overtime_basis`, counted only above `policy.overtime_min_minutes`, and marked pending if `policy.overtime_requires_approval`.
- `incomplete` when no `CLOCK_OUT` exists `policy.incomplete_after_minutes` after shift end. The system never invents a clock-out.
- Precedence (architecture): approved leave > holiday > weekly off > absent. Work on a holiday or weekly off is recorded as worked time with an anomaly flag (`holiday_work`, `weekly_off_work`); what that means for pay is a payroll policy for later.
- Every derived value is stored with the versions and inputs used, so a summary can always be explained and reproduced.

### 4.6 leave

Leave types and policies are Sigvitas configuration. Nothing is seeded in production, and no leave type, accrual amount or leave year is assumed (`product-requirements.md` §6.12).

**leave_types** — `code UNIQUE`, `name`, `is_paid boolean`, `unit` (`day`), `allows_half_day boolean`, `attachment_rule` (`never`, `always`, `after_days`), `attachment_after_days NULL`, `counts_weekly_offs boolean`, `counts_holidays boolean`, `status`.

**leave_policies** — effective-dated rules per leave type, optionally narrowed to a location and/or employment type (most specific wins):
- `leave_type_id`, `location_id NULL`, `employment_type NULL`, `effective_from`, `effective_to`, `version`.
- `leave_year_start_month smallint`, `leave_year_start_day smallint` (e.g. 1/1 or 4/1; Sigvitas decides).
- `accrual_method` (`none`, `upfront_per_leave_year`, `periodic`), `accrual_frequency` (`monthly`, `quarterly`, NULL), `accrual_amount numeric(5,2)`, `accrual_timing` (`period_start`, `period_end`), `prorate_on_joining boolean`, `prorate_on_exit boolean`.
- `carry_forward_cap numeric(5,2) NULL` (NULL = unlimited, 0 = none), `carried_days_expire_after_months NULL`, `year_end_action` (`lapse`, `keep`) for the balance above the cap.
- `allow_negative_balance boolean`, `max_negative_days numeric(5,2) NULL`.
- `EXCLUDE` non-overlap per `(leave_type_id, coalesce(location_id), coalesce(employment_type))`.
- Validated by a Pydantic model; changes audited with old and new values.

**leave_ledger** — append-only balance ledger.
- `employee_id`, `leave_type_id`, `leave_year date` (start date of the leave year the entry belongs to), `entry_type` (`opening_balance`, `accrual`, `usage`, `usage_reversal`, `adjustment`, `carry_forward`, `carry_forward_expiry`, `lapse`), `amount numeric(5,2)` (signed), `effective_date`, `leave_request_id NULL`, `policy_id`, `policy_version`, `import_batch_id NULL` (opening balances), `reason`, `created_by`.
- Balance = `SUM(amount)` for `(employee_id, leave_type_id)` up to a date. Index `(employee_id, leave_type_id, effective_date)`.
- Job idempotency: `UNIQUE (employee_id, leave_type_id, entry_type, accrual_period) WHERE entry_type = 'accrual'`; `UNIQUE (employee_id, leave_type_id, leave_year, entry_type) WHERE entry_type IN ('carry_forward','lapse','opening_balance')`.

**leave_requests** — `employee_id`, `leave_type_id`, `start_date`, `end_date`, `start_half` (`first`, `second`, NULL), `end_half`, `days_requested numeric(5,2)` (computed server-side), `reason varchar(1000)` (personal tier), `attachment_document_id NULL`, `status` (`pending`, `approved`, `rejected`, `cancelled`, `cancellation_requested`), `version`.
- `CHECK (end_date >= start_date)`.
- `EXCLUDE USING gist (employee_id WITH =, daterange(start_date, end_date, '[]') WITH &&) WHERE (status IN ('pending','approved','cancellation_requested'))` — prevents overlaps. Half-day overlaps on the same day (first half + second half) are handled by `leave_request_days` instead if Sigvitas needs them; MVP rejects them.

**leave_request_days** — `leave_request_id`, `date`, `portion numeric(3,2)` (1.0 or 0.5), `counted boolean`, `not_counted_reason NULL` (`weekly_off`, `holiday`) according to the leave type's `counts_weekly_offs` / `counts_holidays`. Makes the calculation explicit and reportable.

**leave_approvals** — `leave_request_id`, `step smallint`, `approver_user_id`, `decision` (`approved`, `rejected`), `note`, `decided_at`. MVP has one step; the table supports multi-step chains later.

**holiday_calendars** — `name`, `year smallint`, `status`. **holidays** — `calendar_id`, `date`, `name`, `is_optional boolean`, `UNIQUE (calendar_id, date)`. **location_holiday_calendars** — `location_id`, `calendar_id`, `year`, `UNIQUE (location_id, year)`.

### 4.7 payroll (Phase 2)

**salary_components** — `code UNIQUE`, `name`, `kind` (`earning`, `deduction`), `calculation` (`fixed`, `percent_of_component`), `base_component_id NULL`, `is_taxable` (informational until statutory rules exist), `display_order`, `status`.

**compensations** — effective-dated salary per employee. `employee_id`, `effective_from`, `effective_to`, `annual_amount numeric(14,2)`, `currency`, `pay_frequency` (`monthly`), `revision_reason`, `approved_by`, `version`. `EXCLUDE` non-overlap.

**compensation_lines** — `compensation_id`, `component_id`, `amount numeric(14,2)` or `percent numeric(6,3)`, `UNIQUE (compensation_id, component_id)`.

**payroll_periods** — `period_start`, `period_end`, `pay_date`, `status` (`draft`, `in_review`, `finalized`, `published`), `prepared_by`, `submitted_for_review_by`, `finalized_by`, `finalized_at`, `published_at`. `EXCLUDE` non-overlap on the period. `CHECK (finalized_by IS NULL OR finalized_by <> submitted_for_review_by)` enforces four-eyes at the database level too.

**payroll_entries** — `payroll_period_id`, `employee_id`, `compensation_id` (the record used), `paid_days`, `unpaid_days`, `gross`, `deductions`, `net`, `currency`, `UNIQUE (payroll_period_id, employee_id)`. **payroll_entry_lines** — component snapshot lines.

**payslips** — `payroll_entry_id UNIQUE`, `document_version_id` (PDF in private storage), `published_at`, `first_viewed_at`.

After `finalized`, a trigger rejects updates to entries and lines of that period. Corrections are adjustment lines in a later period.

### 4.8 docs

**document_categories** — `code UNIQUE`, `name`, `sensitivity` (`internal`, `personal`, `sensitive`), `employee_visible boolean`, `employee_can_upload boolean`, `requires_expiry boolean`, `retention_days NULL`.

**documents** — `owner_employee_id NULL` (NULL = company document), `category_id`, `title varchar(200)`, `current_version_id NULL`, `expires_on date NULL`, `status` (`active`, `archived`), `deleted_at`, `version`. Index `(owner_employee_id, category_id)`.

**document_versions** — `document_id`, `version_no int`, `storage_key text UNIQUE` (random), `original_filename varchar(255)`, `content_type` (sniffed), `size_bytes bigint`, `sha256 bytea`, `scan_status` (`pending`, `clean`, `infected`, `failed`), `scanned_at`, `uploaded_by`. `UNIQUE (document_id, version_no)`. Only `clean` versions are downloadable.

Views and downloads are recorded in `audit.audit_log`, not a separate table.

### 4.9 notify

**notifications** — `recipient_user_id`, `type` (e.g. `leave.request.submitted`), `subject_type`, `subject_id`, `payload jsonb` (IDs and non-sensitive display text only), `read_at`, `created_at`, `deleted_at`. Index `(recipient_user_id, created_at DESC) WHERE deleted_at IS NULL`.

**notification_preferences** — `user_id`, `type`, `channel` (`in_app`, `email`, `push`), `enabled`. `UNIQUE (user_id, type, channel)`. Security types are not stored here (always on).

**email_outbox** — `user_id → identity.users` (the recipient account; the address is read at send time and never stored here), `template` (`identity.invite`, `identity.password_reset`, `identity.password_changed`, `identity.new_device`, `identity.account_locked`, `identity.recovery_code_used`, `identity.mfa_changed`), `template_data jsonb` (small display values only; keys and values are checked: no secrets, codes, tokens or addresses), `idempotency_key UNIQUE` (one email per event, for example one per lock), `status` (`pending`, `sending`, `sent`, `failed`, `cancelled`), `attempts`, `next_attempt_at`, `lease_expires_at`, `last_error` (an exception class name only), `created_at`, `updated_at`, `sent_at`. Indexes `(next_attempt_at) WHERE status IN ('pending','sending')` and `(user_id, template) WHERE status = 'pending'`.
- Written in the business transaction, so an email exists exactly when its change commits. The worker's dispatcher (every minute) claims up to 20 due rows with `FOR UPDATE SKIP LOCKED` under a 5-minute lease, earliest `next_attempt_at` first and then oldest row (UUIDv7 `id`), so rows due at the same moment are taken oldest first; renders each in its own transaction (creating any link token there, so it exists before the email leaves), sends outside any transaction, and records `sent`, a retry with backoff (1, 2, 4, 8 … minutes, at most an hour), or `failed` after `email.delivery.max_attempts` (5). A row that cannot be rendered counts as a failed attempt; an email that no longer applies (the account was disabled, the invite accepted) is `cancelled`. Failed and cancelled rows stay. Delivery is at least once: a worker that stops after the server accepted an email but before recording it sends it again after the lease.

**announcements** (Phase 2) — `title`, `body_sanitized text`, `published_by`, `published_at`, `expires_at`; **announcement_audiences** — `announcement_id`, `department_id NULL`, `location_id NULL` (NULL/NULL = everyone).

### 4.10 audit

The integrity design (sealing, checkpoints, anchoring, verification, retention tombstones) is specified in `security-architecture.md` §8.1. The tables are:

**audit_log** — partitioned by UTC month on `recorded_at` (database insert time, `DEFAULT now()`), so a row always lands in the partition open at write time. A trigger rejects any other `recorded_at`, so a row cannot be placed in another month's partition, and assigns `id` (`uuidv7()`) itself, overwriting any supplied value, so IDs are unique across partitions. The primary key is `(id, recorded_at)`, because keys on a partitioned table include the partition column. Runtime roles insert through the parent table only; they have no `INSERT` on partitions.
- `id`, `recorded_at`, `occurred_at`, `actor_user_id NULL`, `actor_type` (`user`, `system`, `job`), `session_id NULL`, `grant_request_id NULL` (set when the actor was acting under an elevation or break-glass), `request_id`, `ip inet`, `user_agent`, `action text` (e.g. `leave.request.approved`, `compensation.viewed`), `permission_used text NULL`, `target_type`, `target_id`, `subject_employee_id NULL`, `outcome` (`success`, `denied`, `failed`), `changes jsonb` (field names; old/new values only for non-sensitive fields), `reason text NULL`.
- No hash columns: rows are immutable and are never updated, so hashes live in `chain_links`.
- Indexes: `(subject_employee_id, recorded_at)`, `(actor_user_id, recorded_at)`, `(action, recorded_at)`, `(grant_request_id) WHERE grant_request_id IS NOT NULL`.
- Trigger rejects `UPDATE`/`DELETE`/`TRUNCATE` as a second line of defence behind grants, for every role including the owner. `TRUNCATE` triggers do not propagate to partitions, so each partition gets its own.

**security_events** — partitioned by UTC month on `recorded_at`, keyed and protected like `audit_log`. `recorded_at`, `occurred_at`, `event_type` (`login.succeeded`, `login.failed`, `account.locked`, `mfa.enrolled`, `mfa.reset`, `mfa.recovery_code_used`, `token.reuse_detected`, `session.revoked`, `ratelimit.tripped`, `access.denied_sensitive`, `elevation.requested`, `elevation.approved`, `elevation.break_glass`, `audit.chain_mismatch`, …), `user_id NULL`, `session_id NULL`, `request_id NULL` (correlates with the request log), `email_attempted_hash NULL` (keyed 32-byte hash, not raw email, for unknown accounts), `ip`, `user_agent`, `severity` (`info`, `warning`, `high`), `details jsonb` (flat, non-sensitive values). Indexes `(user_id, recorded_at) WHERE user_id IS NOT NULL` (login history) and `(event_type, recorded_at)`. Same triggers.

**chain_links** — insert-only, partitioned to match the audited table's partition. `stream` (`audit_log`, `security_events`), `partition_key` (e.g. `2026-09`), `position bigint`, `record_id uuid`, `digest_version smallint`, `row_digest bytea`, `link_hash bytea`, `sealed_at`. PK `(stream, partition_key, position)`; `UNIQUE (stream, record_id)`. Written only by the sealer job (`hrms_worker`).

**chain_checkpoints** — insert-only, **not partitioned, never deleted**. `stream`, `checkpoint_no bigint`, `kind` (`daily`, `partition_final`, `retention_tombstone`), `partition_key`, `last_position`, `row_count`, `last_link_hash`, `prev_checkpoint_hash`, `checkpoint_hash`, `signature bytea`, `key_id`, `anchor_object_key`, `archive_sha256 NULL` (tombstones), `retention_policy_version NULL`, `created_at`. PK `(stream, checkpoint_no)`; `UNIQUE (stream, partition_key) WHERE kind IN ('partition_final','retention_tombstone')` per kind. Written by the sealer (daily, final) and by the retention job (tombstone).

User-visible login history (AUTH-7) is a filtered read of `security_events` for the user.

**Partitions.** Month partitions are named `<table>_pYYYY_MM` (for example `audit_log_p2026_10`). `audit.ensure_partitions(months_ahead)` creates missing ones for both streams: it is owned by the migrator (only the owner can attach partitions), runs as `SECURITY DEFINER` with a fixed `search_path`, takes one bounded integer and builds every identifier itself, and only `hrms_worker` may execute it. Partitions are created detached and then attached, which does not block concurrent inserts. The migration creates the current month and three ahead; the hourly `audit.ensure_partitions` job keeps it that way. If a table with a partition's name exists but is not attached, or is attached for another month's range, or if a stream has a default partition (which would silently take rows for months without a partition), the function fails instead of skipping, attaching or accepting it, so the job reports the problem rather than treating the month as covered (revision 0011). A missing partition makes the insert fail, and the audited change rolls back with it, so a change is never committed without its audit row.

**No foreign keys** from audit tables: audit rows record denials against targets that may not exist, and they outlive the rows they describe (retention, anonymization).

### 4.11 app

**settings** — `key text PRIMARY KEY`, `value jsonb` (a number), `version`, `updated_by → identity.users`, `updated_at`. Only overrides are stored: every setting is declared in the code registry (`app/platform/settings.py`) with its type, default, bounds, unit and permission, so a default cannot drift and removing a row restores it. Nothing is seeded. Unknown keys are ignored when read and refused when written; secrets are never settings. Changes are serialized, checked against the rules between settings, need step-up, and are audited with old and new values (settings contain no personal data).

| Key | Default | Bounds | Change permission |
|---|---|---|---|
| `security.lockout.threshold` | 10 attempts | 3-20 | `security.settings.manage` |
| `security.lockout.delay_after` | 5 attempts | 1-19, below the threshold | `security.settings.manage` |
| `security.lockout.window_minutes` | 15 | 5-60 | `security.settings.manage` |
| `security.lockout.duration_minutes` | 15 | 5-120 | `security.settings.manage` |
| `security.login.ip_failure_threshold` | 20 per 5 minutes | 5-200 | `security.settings.manage` |
| `security.session.idle_timeout_minutes` | 30 | 5-240, at most the absolute timeout | `security.settings.manage` |
| `security.session.absolute_timeout_hours` | 12 | 1-24 | `security.settings.manage` |
| `security.invite.ttl_hours` | 72 | 1-168 | `security.settings.manage` |
| `security.password_reset.ttl_minutes` | 30 | 10-60 | `security.settings.manage` |
| `security.trusted_device.lifetime_days` | 90 (chosen default; the architecture leaves it open) | 1-365 | `security.settings.manage` |
| `email.delivery.max_attempts` | 5 | 1-10 | `settings.manage` |

Fixed in code, not settings: the access-token lifetime (15 min), the step-up window (10 min, AUTH-8), the sign-in challenge (5 min, 5 attempts), recovery codes (10), active factors (5), and the per-IP block (15 min).

**idempotency_keys** — `user_id`, `key`, `request_hash`, `response_status`, `response_body jsonb`, `created_at`. `UNIQUE (user_id, key)`. Purged after 24 hours.

**export_jobs** — `requested_by`, `export_type`, `parameters jsonb`, `status`, `row_count`, `storage_key NULL`, `expires_at`, `downloaded_at`.

### 4.12 Phase 2 / Future outlines

- `requests`: `request_types` (form schema as validated JSON), `employee_requests` (`employee_id`, `type_id`, `status`, `assigned_to`, `data jsonb`), `request_comments`, `request_events`.
- `assets`: `asset_categories`, `assets` (`tag UNIQUE`, `serial_number`, `status`), `asset_assignments` (effective-dated, `EXCLUDE` non-overlap per asset).
- `lifecycle`: `checklist_templates`, `checklist_template_tasks`, `employee_checklists`, `employee_checklist_tasks` (`assignee_user_id`, `due_date`, `completed_at`).
- `chat`: `conversations` (`kind` direct/group, `direct_key` unique sorted pair for DMs), `conversation_members` (`joined_at`, `left_at`, `role`), `messages` (`conversation_id`, `sender_user_id`, `body`, `edited_at`, `deleted_at`), `message_attachments` (→ `docs.document_versions` in a chat-only category), `message_reads`. Index `(conversation_id, id)` for cursor pagination.
- `ai`: `embedding_chunks` (`source_type`, `source_id`, `chunk_text`, `embedding vector`, `access_scope jsonb` — e.g. company document vs. owner employee), `assistant_sessions`, `assistant_tool_calls` (mirrored in audit).

## 5. Relationship overview

```
users 1──0..1 employees 1──* employee_jobs *──1 departments / designations / locations
  |                 |            └── manager_employee_id ──> employees (reporting tree)
  |                 ├──1 employee_personal
  |                 ├──* employee_identifiers *──1 identifier_types
  |                 ├──* employee_bank_accounts
  |                 ├──* attendance_events ──0..1 attendance_event_voids
  |                 ├──* attendance_days
  |                 ├──* attendance_corrections 1──* attendance_correction_items
  |                 ├──* shift_assignments *──1 shifts
  |                 |    (attendance_policies resolved by shift → location → organization)
  |                 ├──* leave_requests 1──* leave_request_days, leave_approvals
  |                 ├──* leave_ledger *──1 leave_types 1──* leave_policies
  |                 ├──* compensations 1──* compensation_lines *──1 salary_components
  |                 ├──* payroll_entries *──1 payroll_periods; payroll_entries 1──0..1 payslips
  |                 └──* documents 1──* document_versions
  ├──* sessions 1──* session_tokens
  ├──* user_roles *──1 roles *──* permissions
  |       └──0..1 role_grant_requests (elevation, super_admin assignment, break-glass)
  └──* notifications
audit_log / security_events 1──1 chain_links;  chain_checkpoints (continuous chain per stream)
locations 1──* location_holiday_calendars *──1 holiday_calendars 1──* holidays
```

## 6. Integrity rules that live in the database

1. Non-overlapping effective ranges: jobs, shift assignments, attendance policies, leave policies, bank accounts, compensations, payroll periods, attendance locks, leave requests (active statuses).
2. Unique employee code, work email, user email (case-insensitive).
3. One pending correction per employee per day.
4. Grant-request checks: approver ≠ requester; elevation ≤ 8 h; break-glass ≤ 1 h; no self-granted `user_roles` row without a request.
5. Payroll four-eyes check constraint.
6. Append-only triggers on `attendance_events`, `leave_ledger`, `employee_status_history`, and every table in `audit` (`audit_log`, `security_events`, `chain_links`, `chain_checkpoints`). Job history (`employee_jobs`) cannot be deleted or truncated, and only `effective_to` may change. Role assignments (`user_roles`) cannot be deleted or truncated, and only their revocation may be recorded, once.
7. An active account always keeps a confirmed MFA factor (deferred constraint trigger, §4.1).
8. Finalized payroll immutability trigger.
9. All foreign keys `ON DELETE RESTRICT` except token/child tables that are purely owned (`session_tokens`, `recovery_codes`, `correction_items`, `leave_request_days`), which cascade. Audit tables have no foreign keys (§4.10).

Business rules that depend on time zones, policies or permissions live in the service layer and are covered by tests.

## 7. Time zones and work dates

- Each location has an IANA time zone; an employee's zone is their current job's location zone.
- `work_date` on an attendance event is computed when written: the date (in the employee's zone) of the shift start that the event belongs to. A `CLOCK_OUT` at 02:00 for a 22:00–06:00 shift belongs to the previous date.
- Leave and holiday dates are plain dates interpreted in the employee's location.
- A transfer between zones takes effect from the next work date.

## 8. Indexing and performance notes

- Expected volume at 2,000 employees: ~4 attendance events/employee/day → ~2.9M events/year; ~730k attendance days/year; audit ~5–10M rows/year. All comfortably within single-node PostgreSQL with the listed indexes; audit is partitioned for retention, not speed.
- List endpoints use keyset pagination on `(sort_key, id)`.
- Directory search: `pg_trgm` GIN index on `preferred_name`, `legal_first_name`, `legal_last_name`, `employee_code`. Global search (Phase 2): PostgreSQL full-text search with per-module `tsvector` columns, filtered by the same scope filters.
- Reporting queries run in the worker against the primary for MVP; a read replica can be added without code changes beyond connection routing.

## 9. Retention (proposed, to be confirmed with Sigvitas HR/legal)

| Data | Retention |
|---|---|
| Active employee records | While employed |
| Exited employee core record, job history, payroll, payslips | Per the statutory requirement Sigvitas legal confirms for its jurisdiction, then anonymize. Configurable per data category; no value assumed. |
| Attendance events and days | 3 years after the period, then aggregate and delete events (to confirm) |
| Leave ledger and requests | Same as payroll |
| Documents | Per category `retention_days` |
| Sessions and tokens | Deleted 30 days after expiry/revocation |
| Security events | Proposed 2 years, then removed by partition with a retention tombstone (`security-architecture.md` §8.1) |
| Audit log | Proposed 7 years, then removed by partition with a retention tombstone; optionally archived to encrypted cold storage first |
| Audit chain links | Same as the partition they cover |
| Audit chain checkpoints | Never deleted (small; they prove continuity across removed partitions) |
| Write-once checkpoint anchors | Audit retention + 1 year (object lock, compliance mode) |
| Notifications | 180 days |
| Export files | 24 hours |
| Idempotency keys | 24 hours |
| Chat messages (Future) | Configurable; default to be decided |

## 10. Seed data policy

- Migrations seed only: permission catalog, system roles and their permissions, and required **technical** setting keys (session timeouts, lockout thresholds and similar security settings documented in `security-architecture.md`). No HR policy values are seeded: no shifts, attendance policies, leave types, leave policies, holidays, identifier types, document categories, departments, designations or locations.
- Development seed command creates clearly fake data (`Test` name prefix, `@dev.example` emails, employee codes `DEV-0001`, policies named `TEST – …`). It refuses to run when `APP_ENV=production` and when the database already contains non-dev data.
- Installation bootstrap: a one-time CLI command invites the first **two** super admins (SOD-8 needs two). It refuses to run if any super admin exists. Each invite goes through the full password + MFA enrolment flow. No default passwords, no password-only accounts.

## 11. Migrations: ownership, ordering and deployment

**Ownership**

| Objects | Content owned by | Applied by | Notes |
|---|---|---|---|
| Application schemas (`identity`, `access`, `org`, `people`, `attendance`, `leave`, `payroll`, `docs`, `notify`, `audit`, `app`) | HRMS backend team | Alembic | One linear revision history in `backend/migrations/`. Branches are not allowed; CI fails on multiple heads. |
| Procrastinate objects (job tables, functions, types) | Procrastinate upstream, per pinned release | Alembic | The SQL shipped with the pinned Procrastinate version is applied by Alembic revisions that execute those files unchanged. We never hand-edit Procrastinate objects, and we never run `procrastinate schema --apply` against staging or production. |
| Grants, default privileges, database role settings (`statement_timeout`, `transaction_timeout`) | HRMS backend team | Alembic | Re-asserted after every revision that creates objects; a CI test checks the resulting grants (e.g. audit append-only). |
| Permission catalog and system roles | HRMS backend team (code catalog is the source of truth) | Alembic data revisions | CI test: database catalog equals code catalog after `upgrade head`. |

This keeps one migration runner, one ordering and one audit trail of schema changes. It is not a custom mechanism: Alembic runs SQL, and Procrastinate distributes its schema changes as SQL files for exactly this purpose.

**Procrastinate specifics**

- The initial revision installs the base schema of the pinned Procrastinate version.
- Upgrading Procrastinate is a dedicated commit: bump the pinned version; add Alembic revisions that apply each upstream migration file between the old and new versions, in upstream order. The files are copied into `backend/migrations/vendor/procrastinate/<version>/`, and CI verifies their checksums against the installed package.
- If a Procrastinate release splits a change into steps to run before and after the new worker code is deployed, the "before" revision ships in release N and the "after" revision in release N+1, following the expand/contract rule below.
- Procrastinate objects live in a dedicated `procrastinate` schema (confirmed against 3.10.0 in M1: its SQL uses unqualified names). Revision 0002 applies `schema.sql` with `search_path` set to that schema. `hrms_app` and `hrms_worker` have the database-level `search_path = procrastinate, public`; `public` stays on the path so extension operators (`citext` equality, `pg_trgm`) resolve. Application tables are always schema-qualified.

**Ordering rules**

1. Alembic's revision graph is the only ordering. The Procrastinate base schema comes in the first revisions, before any application revision.
2. Every revision must work with both the currently deployed code (N−1) and the new code (N): **expand/contract**. Add columns as nullable or with defaults, backfill in a separate step, add constraints `NOT VALID` then `VALIDATE`, create indexes `CONCURRENTLY` (in a non-transactional revision), and remove or rename only in a later release after no running code uses the old shape.
3. Each revision sets `lock_timeout` (5 s) so a blocked migration fails fast instead of queueing application traffic.
4. Data migrations that touch many rows run in batches as jobs, not inside a schema revision.

**Deployment order (staging and production are identical)**

1. Build one container image (API, worker and migration job share it). Its digest is what gets promoted.
2. Confirm the database is covered by point-in-time recovery, and record the recovery timestamp.
3. Run the migration job: `alembic upgrade head` as `hrms_migrator`, as a one-off task. Stop the deployment if it fails.
4. Run post-migration checks: single head, catalog sync, grants test.
5. Roll out the worker, then the API. Old and new replicas can coexist thanks to expand/contract.
6. Run smoke tests. Mark the deployment complete.

**Staging before production:** the same image digest must have migrated staging successfully first. Staging holds the production schema (not production data) and synthetic data at production-like volume, so lock behaviour and migration duration are measured before production. Any migration taking more than 30 s on staging needs an explicit plan in its commit message.

**Rollback expectations**

- **Code rollback** is the normal path: redeploy the previous image. Expand/contract guarantees the old code works on the new schema.
- **Schema rollback** in staging and production is never done with `alembic downgrade`. A faulty migration is fixed by a new forward revision.
- `downgrade()` functions are written for application revisions and exercised in CI (`upgrade head → downgrade base → upgrade head` on an empty database) so local development stays fast. The Procrastinate base-schema revision's downgrade drops the `procrastinate` schema so this cycle can reach base; revisions that apply later Procrastinate upgrades are forward-only.
- **Point-in-time restore** is reserved for data corruption or loss. It is decided by the named incident owner and follows the restore runbook, because it discards writes made after the restore point.
