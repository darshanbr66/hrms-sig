# Sigvitas HRMS — Security Architecture

Status: Draft v0.1
Baseline: OWASP ASVS 5.0 Level 2; Level 3 for authentication, session management, payroll and documents.
Related: `threat-model.md`, `authorization-model.md`, `api-architecture.md`

## 1. Principles

1. **The server decides.** Every authorization decision is made in the API. The frontend only reflects decisions.
2. **Deny by default.** An endpoint without an explicit permission requirement fails the test suite (see §12).
3. **Least privilege, including for admins.** System administration, HR administration, payroll and security are separate permission sets.
4. **Assume breach of one layer.** Database grants, encryption of sensitive fields, and audit hash chains still hold if application code has a bug.
5. **Every sensitive read is as important as a write.** Viewing salary, downloading a document and exporting data are audited.
6. **No security through obscurity.** IDs may be guessed; authorization must stop the request anyway.

## 2. Data classification

Every column and file category carries a classification. It drives permissions, logging, encryption and retention.

| Level | Examples | Controls |
|---|---|---|
| `public_internal` | Name, designation, department, work email, photo | Any authenticated employee (directory) |
| `internal` | Reporting line, date of joining, employee code, work location, shift | Self, team, HR |
| `personal` | Date of birth, personal email/phone, address, emergency contacts, leave reasons | Self, HR. Not managers by default. Redacted from logs. |
| `sensitive` | Government ID numbers (types configured by HR), bank account and routing code, medical documents | Self (masked), HR with explicit permission + step-up. Application-level encryption. |
| `restricted` | Compensation, payslips, payroll runs | Self and payroll roles only. Step-up. Every read audited. |
| `secret` | Password hashes, TOTP secrets, token hashes, encryption keys | Never returned by any API. Never logged. |

## 3. Authentication

### 3.1 Account lifecycle

- No public sign-up. HR creates the employee; an account is created in `invited` state (`POST /users`, `user.invite`). An invite grants no role. Linking the account to an employee record gives it that employee's self-service access, so it also needs `employee.lifecycle.manage` covering that employee (HR, not system administrators), and the invite must go to the employee's own work email, so nobody can route another employee's account to a mailbox they control. Resending retires the outstanding link at once; revoking retires it and cancels an invite email not yet sent; disabling an invited account does both.
- Invite token: 256-bit random, stored as SHA-256 hash, single use, 72-hour expiry. The invite flow is one sequence: set password → enrol TOTP (scan, confirm a code) → save recovery codes (shown once) → account becomes `active`. Accepting the invite also verifies the email address. An account never reaches `active` without a confirmed MFA factor.
- The first two super admins are bootstrapped the same way through a one-time CLI invite (two, because assigning `super_admin` later needs a second super admin's approval). There are no default credentials and no password-only bootstrap.
- Deactivation (exit, suspension): account state `disabled`, all sessions and refresh tokens revoked in the same transaction, API keys (none in MVP) revoked.

### 3.2 Passwords

- Hash: Argon2id via `argon2-cffi` (used directly; it provides hashing, verification and rehash detection), m=64 MiB, t=3, p=1, tuned so a hash takes ~250 ms on production hardware. Parameters stored in the hash string; rehash on login when parameters change. Passwords are NFKC-normalized before hashing. Hashing runs in a worker thread outside any database transaction, at most 4 at a time per API process, so a flood of sign-in attempts queues instead of exhausting memory (each hash uses 64 MiB).
- Policy: 12–128 characters, Unicode allowed, no composition rules, no forced periodic rotation (NIST SP 800-63B).
- Breached-password check: a bundled list of common passwords (SecLists NCSC list filtered to 12-128 characters, MIT licence) is always checked; the Have I Been Pwned range API (k-anonymity: only the first 5 characters of the SHA-1 hash leave the server, with response padding) is checked too when `HIBP_ENABLED`. An unreachable range API does not block the password. HTTP client libraries log at WARNING only, so the range URL never reaches the logs.
- Password change requires the current password and revokes all other sessions.

### 3.3 Multi-factor authentication

**MFA is mandatory for every user account, including employees with no other role (ADR-008).** There is no password-only sign-in, no "remember this device" bypass of the second factor, and no setting that disables MFA.

- MVP factor: TOTP (RFC 6238, 30 s step, ±1 step tolerance, a code already used is rejected) plus 10 single-use recovery codes, stored hashed and shown once.
- TOTP secrets are encrypted at the application level (see §6).
- Phase 2: WebAuthn passkeys, which resist phishing, available to every user as a first-class factor. Once available they are **required** for `super_admin`, `system_admin` and `payroll_admin`. TOTP stays available to everyone else.
- A user can hold several factors and may remove one only while another confirmed factor remains. Replacing an authenticator means enrolling the new one first. No endpoint leaves an active account without MFA.
- **Lost factor:** the user signs in with password + a recovery code. That session is restricted to MFA enrolment until a new authenticator is confirmed, and the recovery-code sign-in counts as the verification for that enrolment. Using a recovery code notifies the user by email and raises a security event.
- **MFA reset by an administrator** (no factor and no recovery code left) requires `security.mfa.reset`, step-up by the resetter, a recorded reason, and a documented identity check (process, not code). The reset revokes all of the user's sessions and emails them a single-use re-enrolment link (24 h). Re-enrolment requires the password *and* that link, so a stolen password alone cannot enrol an attacker's authenticator. Until re-enrolment is complete, the account can reach only the enrolment endpoints. Nobody can reset their own MFA (SOD-4).
- Password reset never removes MFA. After resetting the password, the user must still pass MFA or use a recovery code.
- **Password reset** (AUTH-5, T16): the request always answers `202` with the same body and writes a security event either way (with the keyed hash of the email when there is no active account), limited to 3 per hour per email and 10 per hour per IP (failing closed). The reset email goes through the outbox; its 256-bit link token is created by the worker when it sends the email and stored only as a SHA-256 digest, and each sent link retires the earlier ones. Lifetime `security.password_reset.ttl_minutes` (30). Completion checks the policy and the breached list before taking any lock, then, with the token and the account locked: consumes the token, stores the new hash, ends every session and every pending sign-in challenge, revokes every trusted device, records `password_reset.completed`, and emails "your password was changed". It does not sign in and does not touch MFA. A used, expired, replaced or malformed link gets `409 password_reset.invalid`.

### 3.4 Login throttling and lockout

Permanent lockout is a denial-of-service vector, so throttling is progressive. Failed password and failed MFA attempts both count.

| Signal | Response | State stored in |
|---|---|---|
| 5 failed attempts for an account within 15 min | Exponential delay per attempt (1, 2, 4, 8… s, capped at 30 s) | PostgreSQL (`identity.users.failed_login_count`, `failed_login_window_started_at`) |
| 10 failed attempts for an account within 15 min | Account locked for 15 minutes; user notified by email; security event raised | PostgreSQL (`identity.users.locked_until`) |
| 20 failed attempts from one IP within 5 min, any accounts | IP blocked for login for 15 minutes; security event raised | Redis counter (ephemeral), security event in PostgreSQL |
| Successful login from a new device/browser | Email notification to the user | PostgreSQL (`identity.trusted_devices`) |

**Trusted devices.** A browser is recognized by a random 256-bit cookie (`__Host-sv_dev`, HttpOnly, Secure, SameSite=Lax), stored only as its SHA-256 digest with the user agent and times (no IP address, no fingerprinting). Trust is created when a sign-in completes (both factors passed) or an invite is activated, and expires after `security.trusted_device.lifetime_days` (90 by default; use does not extend it). Its only effect is that a sign-in from that browser is not emailed as new: it never skips the second factor or step-up (§3.3, §3.6). Users see and revoke their browsers (`/me/devices`). Trust is revoked for every other browser on a password change and on any MFA change (the browser making the change keeps it), and for every browser on a password reset, an administrator's revocation of the user's sessions, and when the account is disabled.

**Security emails** go through the outbox (`database-design.md` §4.9), never from the request: new device, account locked (one email per lock, however many attempts follow), recovery code used, password changed or reset, authenticator added or removed, recovery codes replaced. They carry no secret, code or token except the one link an invite or reset email exists to deliver, and the browser description in them is reduced to plain characters so attacker-chosen text cannot read as a link or an address.

**Attempted emails.** A sign-in for an unknown account and a reset request for an address without an active account are recorded in `security_events.email_attempted_hash` as HMAC-SHA256 of the canonical email (NFKC, trimmed, lower case; `platform/security/emails.py`) with a dedicated key (`EMAIL_LOOKUP_HMAC_KEY`, different from every other key). Repeated attempts on one address correlate; the address cannot be recovered without the key. The digest is never returned by any API and is kept as long as the security event (`database-design.md` §9).

The thresholds are registry settings (`security.lockout.*`, `security.login.ip_failure_threshold`, `app/platform/settings.py`) with these values as defaults. Account-level lockout is security state, so it lives in PostgreSQL and survives any Redis failure. Per-IP counters are ephemeral. If Redis is unavailable, the auth endpoints fail closed (`503`) rather than silently dropping IP throttling (`architecture.md` §3.1). The thresholds above are security settings, adjustable by `security.settings.manage`.

Responses are identical (`401 invalid-credentials`) for unknown email, wrong password, locked, disabled or not yet activated account, and wrong code, with the timing of a real password verification (a dummy Argon2 verification for unknown accounts). The per-IP block is a 5-minute sliding count of failures in Redis; reaching 20 (`security.login.ip_failure_threshold`) sets a 15-minute block entry. Known limitation: the progressive delay applies only to existing accounts with recent failures, so after five failures an attacker could tell an existing account from an unknown one by timing; the account lockout and the per-IP limit bound such probing.

### 3.5 Sessions and tokens

Opaque server-side tokens, not JWTs, so revocation is immediate and there is no signing-key sprawl (ADR-007).

| Token | Format | Lifetime | Storage (web) | Storage (server) |
|---|---|---|---|---|
| Access token | 256-bit random, base64url | 15 minutes | `__Host-sv_at` cookie: `HttpOnly; Secure; SameSite=Lax; Path=/` | SHA-256 hash in `identity.session_tokens` |
| Refresh token | 256-bit random | Single use; rotated on every refresh | `__Secure-sv_rt` cookie: `HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth/refresh` | SHA-256 hash, linked to previous token |
| Session | Row in `identity.sessions` | Idle timeout 30 min, absolute 12 h (web); configurable | — | device label, IP, user agent, created/last active, step-up time |

- **Rotation and reuse detection.** Each refresh issues a new access and refresh token and marks the old refresh token used. Presenting a used refresh token revokes the entire session and raises a security event (token theft signal).
- **Idle timeout** is based on user activity, not background refreshes: the session's `last_activity_at` updates only on user-initiated requests (the client marks background polling with `X-Sv-Background: 1`, which does not extend activity). It is written at most once a minute. A refresh does not extend it either.
- **Session ID rotation** on login (a new session), on step-up and on confirming a new factor in an enrolment-only session (new access and refresh tokens; the old ones stop working), and on any change to the user's roles (their access tokens end at once, so each session's next refresh issues new tokens).
- **Locking order.** Changes to a session lock the account row, then the session row, then its tokens; a refresh locks the session before its token. Concurrent refreshes, sign-outs and revocations therefore serialize instead of deadlocking, and every locked read refreshes the row it locks.
- **Revocation** is a row update checked on every request. Effective permissions are also resolved from PostgreSQL on every request, so a role removal or an expired elevation takes effect on the next request. There is no permission cache to invalidate.
- **Mobile (future)** uses the same tokens in the response body (with `X-Client-Type: mobile`, not implemented until the mobile client exists), sent as `Authorization: Bearer`, stored in the platform keystore. Mobile absolute lifetime 30 days with device-bound refresh (Phase 2 decision).

### 3.6 Step-up authentication

Some actions require the user to have re-verified their MFA factor within the last **10 minutes**. The session stores `step_up_at`. Re-verification is a TOTP code in the MVP, or a passkey from Phase 2. A password alone never satisfies step-up. Recovery codes cannot be used for step-up; they are only for signing in after losing a device.

Actions requiring step-up (the permission catalog marks these with `step_up: true`):

- Any read of another person's compensation, payslip, or sensitive identifiers.
- Any `*.export` permission.
- Role assignment, role editing, requesting or approving a privilege elevation, MFA reset for others, session revocation for others.
- Changing security or system settings.
- Changing own password, email, or MFA.
- Payroll finalize and publish.

The API returns `403` with problem type `step-up-required`; the client prompts for a TOTP code and retries.

## 4. Authorization

Full model in `authorization-model.md`. Enforcement architecture:

- Permissions are checked in the service layer through one engine (`platform/authz`). Routers declare the required permission as metadata so an automated test can verify every route has one.
- List queries receive a SQL scope filter from the engine (`self` → `employee_id = :me`; `team` → `employee_id IN (reporting subtree)`; `all` → optional department/location restriction from the role assignment). Repositories cannot run list queries without a filter argument.
- Resource checks (`policies.py`) cover ownership, team boundary at the time of the record, state (e.g. cannot approve a cancelled request) and separation of duties.
- Responses are shaped by field tier: a manager's response model for an employee has no personal fields at all, rather than hiding them in the client.

## 5. Web application protections

| Threat | Control |
|---|---|
| CSRF | `SameSite` cookies plus a required custom header (`X-Requested-With: sv-web`) on all unsafe methods; the API rejects cookie-authenticated unsafe requests without it. Same-origin deployment means no CORS preflight is needed or allowed. Origin header checked against the configured app origin. |
| XSS | React escaping; no `dangerouslySetInnerHTML` except in one sanitizer-wrapped component (DOMPurify with a strict allow-list) for announcement rich text. Strict CSP (below). |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY`. |
| SQL injection | SQLAlchemy parameterized queries only. Raw SQL only in reviewed repository functions using bound parameters. Lint rule forbids string-formatted SQL. |
| Mass assignment | Pydantic request models with `extra="forbid"`, one model per actor type. `role`, `status`, `employee_id` never accepted on self-service endpoints. |
| IDOR | UUIDv7 IDs are not treated as secret; every fetch runs through resource policy. Tests for cross-user access on every resource endpoint. |
| Open redirect | Post-login redirect accepts only relative paths from an allow-list of app routes. |
| Host header poisoning | Links in emails built from the configured `APP_BASE_URL`, never from the request. Proxy rejects unknown hosts. |
| CSV/formula injection | Export writer prefixes cells starting with `= + - @ \t \r` with a single quote. |
| Request smuggling / large bodies | Proxy limits: 1 MB JSON bodies, 20 MB upload bodies (upload endpoint only), timeouts. |

Security headers (set by the API for API responses and by the proxy for the SPA):

```
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data: blob: <storage-origin>; connect-src 'self' <storage-origin>;
  font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self';
  frame-ancestors 'none'; upgrade-insecure-requests
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Permissions-Policy: camera=(), microphone=(), geolocation=(self), payment=()
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Cache-Control: no-store   (all authenticated API responses)
```

Fonts are self-hosted so no third-party origins are needed in the CSP. Tailwind output is a static stylesheet, so `style-src 'self'` works without `unsafe-inline`. Styles set from JavaScript through the CSSOM (as React, React Aria and Motion do) are not affected by CSP; libraries that inject `<style>` elements at runtime (most CSS-in-JS) are not used.

## 6. Data protection

### 6.1 In transit

- TLS 1.2+ (prefer 1.3) at the proxy; HTTP redirects to HTTPS; HSTS preload.
- TLS between the API/worker and PostgreSQL, Redis and object storage (`sslmode=verify-full` for PostgreSQL).

### 6.2 At rest

- Database and object storage encrypted at rest by the provider (disk level). This protects against lost disks, not against a compromised application or a leaked database dump.
- **Application-level encryption** for `sensitive` and `secret` fields: government IDs, bank account numbers, TOTP secrets. AES-256-GCM via `cryptography`, envelope encryption: a data key per record type encrypted by a key-encryption key held in the secret manager / KMS. Ciphertext stores a key version for rotation. In M1 the keys are supplied as `FIELD_ENCRYPTION_KEYS` (version -> 32-byte key) from the secret manager, with `FIELD_ENCRYPTION_ACTIVE_VERSION`; each ciphertext's associated data binds it to its purpose and owner (TOTP secret: `identity.mfa_factors.secret:<user id>`), so it cannot be moved to another row. Wrapping the data keys with a KMS key-encryption key is part of the hosting decision (ADR-023).
- **Blind index** (HMAC-SHA256 with a separate key) where equality lookup on an encrypted field is needed (e.g. detecting a duplicate government ID number across employees).
- Compensation amounts are not column-encrypted because payroll must compute and aggregate them. They are protected by permissions, step-up, read auditing, a separate PostgreSQL schema, and database-level grants (see `database-design.md` §3).
- Backups encrypted with a key separate from the production database credentials.

### 6.3 Object storage

- Buckets private; account-level public-access block on.
- Object keys are random (`documents/{uuid}`), never containing names or employee codes.
- Downloads: API authorizes, writes audit record, returns a presigned GET URL valid 60 seconds with `response-content-disposition=attachment` and a fixed content type. The URL is used immediately by the client and never stored.
- Uploads go through the API (not presigned PUT) so the API can enforce size, type sniffing and quarantine. Files land in a `quarantine/` prefix; the worker scans with ClamAV and moves clean files to `clean/`. Infected files are deleted and a security event raised.
- Allowed types (MVP): PDF, PNG, JPEG, WebP, DOCX. Detected by magic bytes. Images re-encoded to strip metadata (EXIF location). Office files with macros rejected.
- Export files and payslips expire by lifecycle rules.

### 6.4 Secrets

- All secrets from environment variables injected by the platform secret manager. `.env` files for local development only, git-ignored; `.env.example` lists names with no values.
- Secret scanning (gitleaks) in pre-commit and CI.
- Separate keys per purpose: token hashing pepper (if used), field encryption KEK, blind index key, HMAC key for Redis key names, HMAC key for attempted email addresses, audit checkpoint signing key (Ed25519, worker only), storage credentials, write-once anchor bucket credentials (write-only), database credentials per role, Redis credentials, email credentials. Each rotatable independently. Rotating the audit signing key keeps old public keys, recorded with their `key_id` on each checkpoint, so old checkpoints stay verifiable.

## 7. Logging and PII

- Logs contain IDs, not names, emails or content. Request/response bodies are never logged.
- Redaction filter drops keys matching `password|token|secret|otp|code|authorization|cookie|iban|account|pan|aadhaar|salary|amount` and any field tagged `personal`/`sensitive`/`restricted` in schemas.
- Query parameters of export and document URLs are stripped in proxy logs (signed URLs).
- Log retention 30 days (application), audit and security events per `database-design.md` retention table.

## 8. Audit

Two append-only stores in the `audit` schema:

- `audit.audit_log` — business actions and sensitive reads (who, what, which record, which employee, before/after field names, outcome, request ID, session, IP).
- `audit.security_events` — authentication and security signals (login success/failure, lockout, MFA changes, token reuse, permission denials on sensitive resources, rate-limit trips).

Guarantees:

1. Written in the same transaction as the change (a failed audit write rolls back the change).
2. Denied attempts on sensitive resources are also recorded, in a separate short transaction, so a failed request still leaves a trace.
3. The application roles have `INSERT` and `SELECT` only, and `INSERT` only on the parent tables (never directly into a partition), so every row passes through partition routing and the triggers. `UPDATE`, `DELETE`, `TRUNCATE` are not granted, and append-only triggers back up the grants for every role, including the owner. The database assigns `id` (UUIDv7, overwriting any supplied value) and `recorded_at` (any other value is rejected), so a row cannot be placed in another month's partition or reuse another row's ID. Retention removes whole expired partitions only, through a separate restricted role and the tombstone procedure in §8.1.
4. Tampering, deletion, insertion and reordering are detectable through the sealed hash chain, signed checkpoints and write-once anchors described in §8.1.
5. Sensitive values are not copied into the audit log. For a salary change the audit row records which fields changed and the compensation record ID; the values live in the compensation history, protected by payroll permissions. This avoids turning `audit.read` into a backdoor to salary data.

Events audited (minimum): login/logout, failed login, lockout, password change/reset, MFA enrol/remove/reset, recovery-code use, session revocation, role and permission changes, role assignments, privilege elevation (request, approval, rejection, activation, expiry, revocation, break-glass), employee create/update/status change, personal/sensitive field reads by non-self, compensation read/create/update, payroll state changes, payslip view/download, document upload/view/download/delete, attendance corrections (request, decision), attendance and leave policy changes, leave decisions and balance adjustments, exports (request and download), settings changes.

### 8.1 Audit integrity: sealing, checkpoints and retention

**Goal.** Detect any modification, insertion, deletion, reordering or truncation of audit records, and any removed partition, including by someone with database superuser access, while still allowing retention to delete expired partitions and allowing verification every day.

**Streams.** `audit_log` and `security_events` are two independent streams. Each has its own chain. The design below applies to each stream separately.

**Level 1: row chain inside a partition.**

- Audit rows are inserted by the business transaction with no hash columns. Rows are never updated, so hashes cannot be written back onto them.
- A single **sealer** job (Procrastinate periodic task, every minute, one instance guaranteed by a PostgreSQL advisory lock, running as `hrms_worker`) reads committed, unsealed rows and appends them to the chain in `audit.chain_links`, which is insert-only:
  - `row_digest = SHA-256(canonical_encoding(row))`, where the encoding is RFC 8785 JSON canonicalization of every column including `id` and `recorded_at`, prefixed with a `digest_version`.
  - `link_hash[n] = SHA-256(link_hash[n-1] || row_digest[n] || position[n])`.
  - The chain **restarts in each partition**. Position 1 of partition *P* uses `genesis(P) = SHA-256("sv-audit-genesis" || stream || P)`. It does not depend on the previous partition, so the previous partition can later be deleted without breaking this one. Partitions are linked at Level 2 instead.
- Partitioning is by `recorded_at` (database insert time), so a row always lands in the partition that is open when it is written. Chain order is sealing order, which is recorded as `position`.
- Rows can stay unsealed for up to about a minute. Unsealed rows older than 10 minutes raise an alert.
- Record identity: the database assigns every row's `id`, so `id` alone identifies a row across partitions and `chain_links` can be unique on `(stream, record_id)`. A row becomes visible only when its transaction commits, which can be later than its `recorded_at` (transaction start), so the sealer finds unsealed rows by their absence from `chain_links`, not by a `recorded_at` watermark.

**Level 2: checkpoint chain across partitions and time.**

`audit.chain_checkpoints` is insert-only, never partitioned and never deleted. It holds one continuous chain per stream:

```
partition 2026-09                 partition 2026-10
 rows → links → last link          rows → links → last link
           │                                │
           ▼                                ▼
 … → CP(daily, 09-29) → CP(daily, 09-30) → CP(final, 2026-09) → CP(daily, 10-01) → …
                                               ▲
                     years later: CP(tombstone, 2026-09) appended to the same chain
```

Each checkpoint records `stream`, `checkpoint_no`, `kind` (`daily` | `partition_final` | `retention_tombstone`), `partition_key`, `last_position`, `row_count`, `last_link_hash`, `prev_checkpoint_hash`, `created_at`, and

`checkpoint_hash = SHA-256(prev_checkpoint_hash || kind || stream || partition_key || last_position || row_count || last_link_hash || created_at)`.

- **Daily checkpoint** for every partition that received rows since the previous checkpoint.
- **Partition-final checkpoint** once a month partition can no longer receive rows. That is the month boundary plus a safety margin longer than the maximum transaction duration. Application roles have `transaction_timeout` set (API 30 s, worker 10 min), so the margin is one hour. These are role defaults that a session can change, and the migrator and superusers have none, so the margin is not a hard database guarantee; it does not need to be. After the final checkpoint, a row appearing in that partition is by definition tampering and is reported, never sealed.
- **Signature:** each checkpoint is signed with an Ed25519 key available only to the worker through the secret manager. The key is never stored in the database, and the API process never holds it. A database superuser can recompute SHA-256 chains but cannot produce valid signatures.
- **External anchor:** after signing, each checkpoint is appended as a small JSON object to a dedicated bucket with object lock in compliance mode. The retention period is the audit retention plus one year, and not even the account owner can shorten it. Credentials for this bucket can write new objects only; they cannot delete objects or change retention.

**Daily verification** (`audit.verify`, worker, read-only on the audit schema):

1. For every partition with a checkpoint since the last run, recompute row digests and links from the stored rows and compare them to `chain_links`.
2. Check every checkpoint: `last_link_hash` matches the recomputed link at `last_position`; `row_count` matches; signatures are valid; `prev_checkpoint_hash` continuity has no gaps in `checkpoint_no`; each checkpoint matches its anchored copy in the write-once bucket.
3. Check coverage: every partition referenced by a checkpoint either exists or has a `retention_tombstone`. Every existing partition has a genesis and checkpoints. No partition has rows after its `partition_final` checkpoint.
4. A full re-verification of all retained partitions runs monthly. Daily runs cover recent partitions only.
5. Any mismatch raises a high-severity alert to super admins and the auditor role, and is recorded as a security event.

| Tampering | How it is detected |
|---|---|
| Row modified | Row digest ≠ stored link input; link chain breaks |
| Row deleted or inserted | Link chain breaks; `row_count` ≠ checkpoint |
| Tail truncated | `last_position` in the checkpoint no longer exists |
| Whole partition dropped outside retention | Partition referenced by checkpoints, missing, and no tombstone |
| Chain and checkpoints rewritten by a DB superuser | Signatures fail; anchored copies differ |
| Checkpoint row deleted | Gap in `checkpoint_no`; anchored copy exists without a DB row |

**Retention without breaking integrity:**

1. Only partitions older than the retention period for their stream (`database-design.md` §9) are eligible, and only if their `partition_final` checkpoint exists and verifies.
2. The retention job, running as `hrms_audit_retention`, first verifies the partition in full. If required, it exports the partition to encrypted cold storage and records the export file's SHA-256.
3. It appends a signed, anchored `retention_tombstone` checkpoint that references the partition's final checkpoint hash, row count, retention policy version and archive hash (if any).
4. Only then does it detach and drop the partition and its `chain_links` partition. Only a table's owner can detach a partition, so this runs through a `SECURITY DEFINER` function owned by the migrator and executable only by `hrms_audit_retention`, built like `audit.ensure_partitions` (`database-design.md` §4.10). The append-only triggers act on rows and `TRUNCATE`, not on detaching or dropping a partition, so they do not block it.
5. Verification treats a partition with a valid tombstone as legitimately removed. The checkpoint chain itself is never deleted, so continuity across the removed period can still be proven. An archived partition can be re-verified against its final checkpoint at any time.

**Accepted limitation:** a superuser who alters a row in the minute before it is sealed is not detected by the chain. Provider-level database audit logging and restricted superuser access (§10) cover that window.

The same design applies to both streams, so security events are protected like business audit records.

## 9. Administrative safeguards

- **Separation of duties** (enforced in code, tested; full list in `authorization-model.md` §6): cannot approve own requests; cannot edit own compensation; cannot assign roles to self; cannot approve own elevation; payroll finalization requires a different person from the one who moved the period to review.
- **Super admin is not "see everything".** `super_admin` holds role and permission management, privilege elevation workflow, break-glass recovery, and read access to audit and security events. It holds **no** access to compensation or payroll, employee personal or sensitive data, documents, or private messages. Full definition in `authorization-model.md` §4.2. There must be at least two and preferably no more than three super admins. Super admins must use passkeys once Phase 2 ships.
- **Controlled elevation.** A super admin who genuinely needs another role (for example, `payroll_admin` to fix a payroll configuration) cannot assign it to themselves (SOD-3). They submit an elevation request with the role, reason and duration (maximum 8 hours; default 2). It requires step-up and **approval by a different super admin**, who must also step up. Approval creates a time-bound role assignment that expires automatically. Every step is audited, and all super admins are notified when a request is made, approved, activated, expires or is revoked.
- **Break-glass (emergency only).** When no second super admin can approve (for example, the other super admin is unreachable and all system admins are locked out), a super admin may self-activate **only** the `system_admin` role, for a maximum of 1 hour, with step-up and a mandatory reason. This recovers access (unlocking accounts, resetting MFA for an administrator, revoking sessions) without reaching salary, sensitive data, documents or messages. Break-glass sends an immediate high-severity notification to all super admins and auditors. It is recorded as a security event and appears on a post-incident review list that another super admin must acknowledge.
- **Total lock-out recovery.** If every super admin has lost access, recovery is an infrastructure procedure: a CLI command run in the production environment by two engineers with production access. It issues a new super admin invite, and the full invite + MFA enrolment flow still applies. The runbook, the cloud audit log and a security event record it. There is no hidden in-app master account.
- **Offboarding.** Status change to `exited` disables the account at the effective date/time via a scheduled job, and immediately if HR selects "revoke now".
- **Periodic access review.** Phase 2 report listing all users with non-employee roles, for quarterly review.

## 10. Infrastructure security

- Only the proxy is publicly reachable. Database, Redis, ClamAV and worker are on a private network.
- Database roles: `hrms_migrator` (DDL, used only by the migration job), `hrms_app` (DML with restricted grants), `hrms_worker` (same as app, plus insert-only on audit chain tables), `hrms_audit_retention` (partition detach/drop in `audit` only), `hrms_readonly` (optional, for reporting replicas, no access to `payroll`, `audit`, `identity` or sensitive tables). Full grants in `database-design.md` §3.
- Redis runs on the private network with TLS, an ACL-restricted user, no persistence, and TTL on every key (`architecture.md` §3.1).
- Containers run as non-root with read-only root filesystems; images pinned by digest; base images scanned in CI.
- Production access by engineers via SSO + MFA to the cloud console; direct database access is exceptional, logged, and time-bound.
- Dependency updates weekly, reviewed and committed to `main` like any other change; `pip-audit` and `pnpm audit` fail CI on known high/critical vulnerabilities.

## 11. Privacy

- Data minimization: fields not needed are not collected (e.g. no religion, caste, marital status unless Sigvitas has a documented legal need).
- Purpose and retention recorded per data category (`database-design.md` §9).
- Employees can view all personal data held about them (self-service profile + documents) and request correction.
- Exited employee data retained per legal retention requirements, then anonymized or deleted by a scheduled job; audit keeps IDs only.
- If Sigvitas is subject to India's DPDP Act 2023 and its Rules, privacy notice, breach notification and grievance contact processes need owners outside engineering. Flagged as an open question.

## 12. Security verification

| Activity | When |
|---|---|
| Unit tests for authz policies (every permission × scope × separation-of-duty rule) | Every commit |
| Route coverage test (`tests/security/test_route_coverage.py`): every route declares exactly one access rule: a permission, the account allow-list (the actor's own account: `/me`, `/me/password`, `/me/mfa/*`, `/auth/logout`, `/auth/step-up`), or the public allow-list (`/auth/login`, `/auth/login/mfa`, `/auth/refresh`, `/auth/password-reset/*`, `/auth/invite/*`, `/auth/mfa-reenrolment/*`, `/health/*`). Every catalog permission is routed or listed with the checkpoint that brings it. | Every commit |
| Authorization matrix integration tests: each role against each endpoint with own/team/other records | Every commit |
| SAST: Ruff security rules (flake8-bandit), Semgrep; ESLint security plugins | Every commit |
| Dependency and container scanning | Every commit + weekly |
| Secret scanning | Pre-commit + every commit |
| DAST: OWASP ZAP baseline against staging | Every release |
| Manual review of auth, authz, payroll, document and audit code by a second engineer (or, when the project owner authorizes it because none is available, a recorded adversarial self-review plus the full automated suite, with the human review still owed) | Before every push that touches them (`Reviewed-by:` trailer only for a human review, `engineering-principles.md` §8) |
| External penetration test | Before production launch, then annually |
| Restore test from backup | Quarterly |
| Audit chain verification (recent partitions) | Daily job, alert on mismatch |
| Full audit chain + anchor re-verification | Monthly job |
| Tamper tests: modify, delete, insert, drop partition, rewrite chain without signing key, all against a test database | Every commit (integration suite) |
| MFA invariants: no path activates an account or completes sign-in without a confirmed factor | Every commit |
