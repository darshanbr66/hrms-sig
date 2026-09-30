# Sigvitas HRMS — Product Requirements

Status: Draft v0.1 (architecture phase, nothing implemented)
Owner: Sigvitas engineering
Last reviewed: 2026-09-30

## 1. Purpose

Sigvitas HRMS is the internal system Sigvitas uses to manage its people: who works here, how they are organized, when they work, when they are away, what they are paid, and the documents and requests that go with employment.

It replaces spreadsheets, email threads and ad-hoc tools with one system that:

- keeps confidential HR data inside clearly enforced access boundaries,
- gives every employee direct access to their own information,
- gives managers what they need to run their team and nothing more,
- gives HR a reliable record with a full history of who changed what.

It is an internal tool for one organization (Sigvitas). It is not a multi-tenant SaaS product. See ADR-003.

## 2. Users

| User | What they need most | What they must never see by default |
|---|---|---|
| Employee | Clock in/out, breaks, leave balance and requests, own profile, own payslips and documents, requests to HR | Other employees' personal data, anyone's salary |
| Manager | Team attendance, approvals (leave, corrections), team calendar, team profile basics | Team members' salary, personal/sensitive data, private messages |
| HR | Employee records, lifecycle changes, attendance and leave administration, documents, HR requests | Salary (unless also given the payroll role), security configuration, private messages |
| HR Admin | Everything HR has, plus policy configuration (shifts, attendance policies, leave types and policies, holiday calendars, identifier types, document categories) | Salary (unless also payroll), security configuration |
| Payroll Admin | Compensation records, payroll periods, payslips | Security configuration; nothing beyond payroll unless granted |
| System Admin | User accounts, sessions, system settings, integrations, security events | Salary, employee sensitive data, documents, private messages |
| Super Admin | Role and permission management, controlled privilege elevation, break-glass recovery | Salary, payroll, employee personal and sensitive data, documents, private messages. Any data access comes only through a time-bound elevation that another super admin approves after step-up. Every elevation is audited and announced to all super admins. |
| Auditor (optional) | Read-only audit log and security events for compliance review | Employee data, salary, documents, messages. The audit log contains no sensitive values. |

"Admin" never implies full data access. See `authorization-model.md`.

## 3. Product principles

1. **Correct before clever.** HR data drives pay and legal records. A slower, correct workflow beats a fast wrong one.
2. **History is never lost.** Changes to attendance, leave, compensation, job details and permissions create new records. Old values remain queryable.
3. **Least privilege by default.** A new role, feature or report grants nothing until someone grants it on purpose.
4. **Self-service first.** If an employee can safely do something themselves, they should not have to ask HR.
5. **Calm, plain interface.** No marketing language, no invented numbers, no decoration that slows people down.
6. **Works on a phone.** Clocking in, applying for leave and approving requests must be comfortable on a small screen.
7. **Works without AI.** Any future AI feature is an optional layer on top of the same permissions.

## 4. Scope by phase

Detailed sequencing lives in `development-roadmap.md`. This table is the product-level scope.

| # | Module | MVP | Phase 2 | Future |
|---|---|---|---|---|
| 1 | Identity and authentication (password + mandatory TOTP MFA for every account, sessions, invites, reset) | Yes | Passkeys, SSO (OIDC) | Mobile device binding |
| 2 | Authorization and permissions (incl. super admin elevation and break-glass) | Yes | Custom roles UI, approval delegation, access review report | |
| 3 | Employee management (core record, job history) | Yes | Profile change approval workflow | Custom fields |
| 4 | Organization (departments, designations, locations, reporting lines) | Yes | Org chart view | Cost centres |
| 5 | Employee self-service (own profile, own data) | Yes | | |
| 6 | Attendance (event-based clock in/out) | Yes | Geo/IP policies | Biometric device import |
| 7 | Break management (break rules via attendance policy) | Yes | | |
| 8 | Attendance corrections / regularization | Yes | Bulk correction by HR | |
| 9 | Shift management and attendance policies | Versioned shift definitions, assignments, effective-dated attendance policies (all values configured by HR) | Rotations, rosters | Auto-scheduling |
| 10 | Leave management | Yes | Comp-off, encashment | |
| 11 | Holiday calendar | Yes | Optional/restricted holidays | |
| 12 | Payroll / salary (compensation records) | | Yes | Statutory calculations for Sigvitas's jurisdiction |
| 13 | Payslips | | Yes (publish + secure download) | Generated from payroll engine |
| 14 | Documents | Yes (employee documents) | Versioning UI, expiry reminders | E-signature |
| 15 | Notifications (in-app + email) | Yes | Push (mobile), digest | |
| 16 | Announcements | | Yes | Acknowledgement tracking |
| 17 | Private messaging / chat | | | Yes (see §6.7) |
| 18 | Employee requests (to HR) | | Yes | |
| 19 | Assets | | Yes | |
| 20 | Employee lifecycle (status, transfers, promotions) | Core status + job changes | Full timeline view | |
| 21 | Onboarding | Invite + account activation | Checklists and tasks | |
| 22 | Offboarding / exit | Deactivation with immediate session revocation | Exit checklist, asset recovery | |
| 23 | Reports | Attendance and leave reports, CSV export | Headcount, payroll reports | Scheduled reports |
| 24 | Audit logs | Yes | Audit viewer filters, export | External log shipping |
| 25 | Security center | Sessions, login history, security events | Alerting rules | |
| 26 | System settings | Yes (typed settings) | | |
| 27 | Search | Employee directory search | Global search across modules | Natural-language search |
| 28 | AI assistant | | | Yes (see `architecture.md` §9) |

Chat is placed in Future deliberately. It is the module with the highest privacy risk and the lowest dependency from the rest of the HRMS, and Sigvitas almost certainly already uses a messaging tool. It should be built only if there is a clear reason to move HR conversations into this system. This is listed as an open question.

## 5. MVP definition

The MVP is done when Sigvitas can run day-to-day people operations in this system for all employees:

1. HR creates an employee record and sends an invite. The employee activates their account, sets a password and enrols in MFA.
2. Every employee can clock in, take breaks, clock out, and see their own daily and monthly attendance.
3. An employee who forgot to clock out can submit a correction; their manager approves or rejects it; the original events remain visible.
4. Employees apply for leave against the leave types Sigvitas has configured and see balances. Managers approve. Day counting follows each leave type's configured rules for weekly offs and holidays.
5. Managers see their team's attendance and leave, and nothing beyond their team.
6. HR maintains departments, designations, locations, reporting lines and job history.
7. Employee documents (ID proofs, letters) are uploaded to private storage and downloaded only through authorized, short-lived links.
8. Users receive in-app and email notifications for events that need their action.
9. Every sensitive action is written to the audit log, and security events are visible to authorized staff.
10. HR can export attendance and leave reports, with step-up authentication and an audit record.
11. When an employee leaves, one action deactivates the account and revokes all sessions immediately.

Salary and payslips are the first Phase 2 module (see roadmap), not MVP, because they require the permission, audit and document foundations to be proven first.

## 6. Functional requirements by module

Requirement IDs are stable and referenced from tests and ADRs.

### 6.1 Identity and authentication

- AUTH-1 No self-registration. Accounts exist only for invited people.
- AUTH-2 Invite link is single-use, expires in 72 hours, and leads through setting a password and enrolling MFA. The account becomes active only when both are complete. Accepting an invite verifies the email address.
- AUTH-3 Login with work email + password. Passwords are 12–128 characters, checked against a breached-password list, no composition rules (NIST SP 800-63B).
- AUTH-4 MFA is mandatory for every user account, including employees with no other role. There is no password-only sign-in. MVP: TOTP plus 10 single-use recovery codes. Phase 2: passkeys (WebAuthn) for everyone, required for super admins, system admins and payroll admins.
- AUTH-4a A user can never remove their last MFA factor. A lost device is recovered with a recovery code, or by an administrator MFA reset that requires re-enrolment through an emailed link plus the password.
- AUTH-5 Password reset via emailed single-use link, 30-minute expiry. Reset revokes all sessions. The response never reveals whether an email exists.
- AUTH-6 Users see their active sessions (device, approximate location from IP, last active) and can revoke any of them.
- AUTH-7 Users see their own login history for the last 90 days.
- AUTH-8 Sensitive actions require step-up (re-verifying the MFA factor) within the last 10 minutes. A password alone never satisfies step-up.
- AUTH-9 Changing email requires verification of the new address and notification to the old address.

### 6.2 Authorization

- AUTHZ-1 Permissions are fine-grained and scoped (`self`, `team`, `all`). See `authorization-model.md`.
- AUTHZ-2 Roles are bundles of permissions. Role assignments can be restricted to a department or location.
- AUTHZ-3 Nobody can approve their own request, change their own compensation or change their own roles.
- AUTHZ-4 Granting a role that contains a sensitive permission requires a super admin, step-up authentication, and notifies all super admins. Granting `super_admin` also requires a second super admin's approval.
- AUTHZ-5 All checks run on the server. The UI hides actions the user cannot perform, but that is a convenience, not a control.
- AUTHZ-6 Super admins have no standing access to salary, payroll, personal or sensitive employee data, documents or messages. They obtain a data role only through a time-bound elevation (maximum 8 hours) approved by a different super admin, with step-up on both sides, full audit and notification to all super admins.
- AUTHZ-7 Break-glass: when no second super admin is available, a super admin may self-activate only the `system_admin` role (account recovery, no employee data) for up to 1 hour, with step-up, a reason, and immediate high-severity notification.

### 6.3 Employees and organization

- EMP-1 Employee record: employee code, legal name, preferred name, work email, phone, date of joining, employment type, status.
- EMP-2 Job details are effective-dated: department, designation, location, reporting manager, employment type. A change creates a new row; history stays.
- EMP-3 Personal details (date of birth, personal contact, address, emergency contacts) are a separate tier with separate permissions.
- EMP-4 Sensitive identifiers (government IDs, bank account) are encrypted at the application level and shown masked unless the viewer holds the permission and has stepped up. Which identifier types are collected is configuration (an identifier-type catalogue), decided by Sigvitas HR under the minimization rule. None is assumed.
- EMP-5 Employee statuses: `pre_joining`, `active`, `on_notice`, `exited`. `exited` disables login.
- EMP-6 Directory: all active employees can see name, preferred name, designation, department, location, work email and photo. Nothing else.
- EMP-7 Employees may edit a defined subset of their own fields. Phase 2: some fields require HR approval before taking effect.

### 6.4 Attendance, breaks, shifts, corrections

- ATT-1 Attendance is recorded as events: `CLOCK_IN`, `BREAK_START`, `BREAK_END`, `CLOCK_OUT`. Logging in to the HRMS is not clocking in (see ADR-011).
- ATT-2 Event time is the server's time. Client-reported time is stored only as metadata.
- ATT-3 Events must follow a valid sequence (no break without clock-in, no double clock-in).
- ATT-4 Daily summary derived from events by applying the employee's shift and the attendance policy effective on that date: first in, last out, gross duration, break duration, effective work duration, late arrival minutes, early departure minutes, overtime minutes, status. Whether and how each of these is computed is set by policy (§6.12).
- ATT-5 Day status values: `present`, `half_day`, `absent`, `on_leave`, `holiday`, `weekly_off`, `incomplete` (missing clock-out), `pending_correction`. The status vocabulary is architecture; the thresholds that assign `present` versus `half_day` versus `absent` are policy values.
- ATT-6 A day with a missing clock-out is marked `incomplete`. The system never invents a clock-out time.
- ATT-7 Corrections: the employee proposes added or voided events with a reason; the approver sees original and proposed timelines side by side. Approval appends correction events and voids (never deletes) originals.
- ATT-8 HR can record attendance on behalf of an employee only through the same correction mechanism, with a reason.
- ATT-9 A **shift** defines *when* work is expected: start time, end time and weekly off days. Shifts may cross midnight; the work date is the date on which the shift starts. An **attendance policy** defines *how* the day is judged: grace period, late arrival and early departure rules, full-day and half-day thresholds, break limits, overtime rules, and when a day becomes incomplete. Both are effective-dated records that HR configures. The system ships with no production shift or policy values.
- ATT-10 Summaries are recomputed when events, corrections, shift assignments, attendance policies, leave or holidays change for that day. A past day is always evaluated with the shift and policy versions effective on that day.
- ATT-12 Attendance for a location is not computed until a shift and an attendance policy exist for it. The setup screen shows what is missing instead of applying hidden defaults.
- ATT-11 Attendance for a closed payroll period is locked. Corrections to locked days require HR with `attendance.correction.approve.all` and are flagged in the audit log.

### 6.5 Leave and holidays

- LV-1 Leave types are configured by Sigvitas HR; none are built in and none are assumed. Each type and its effective-dated policy carry the rules listed in §6.12: accrual, opening balances, carry-forward, lapse, negative balance, half-day support, attachment requirements, day-counting rules and the leave year.
- LV-2 Balances are a ledger (opening balance, accrual, usage, reversal, adjustment, lapse, carry-forward). The balance is the sum of entries. No mutable balance number. Opening balances are imported as ledger entries with a reference to the import.
- LV-3 Requests support date ranges, a reason, and half days where the leave type allows them. Whether weekly offs and holidays inside the range count toward the leave is a per-leave-type policy setting, evaluated against the employee's shift and holiday calendar.
- LV-4 Overlapping pending or approved requests for the same employee are rejected by a database constraint.
- LV-5 Approval by reporting manager in MVP. Multi-level approval chains in Phase 2.
- LV-6 Employees can cancel pending requests, and request cancellation of approved future leave.
- LV-7 Holiday calendars per location. Each location has one calendar per year.
- LV-8 Team leave calendar visible to managers (team scope). Phase 2 option: peers in the same department see colleagues as "away" with no leave type or reason (`leave.calendar.read.department`).

### 6.6 Payroll and payslips (Phase 2)

- PAY-1 Compensation is effective-dated: annual amount, currency, pay frequency, component breakdown. Ranges may not overlap.
- PAY-2 Components are configurable (earning or deduction, fixed amount or percentage of another component). No statutory calculations for any country (for example Indian PF, ESI, PT or TDS) until Sigvitas explicitly requests them.
- PAY-3 Payroll periods move through `draft → in_review → finalized → published`. Finalized periods are immutable; corrections happen in a later period as adjustments.
- PAY-4 Finalization requires a second payroll admin (four-eyes), configurable but on by default.
- PAY-5 Payslips are snapshots: the line items as they were at finalization, not a live view of current compensation.
- PAY-6 Payslips are delivered as PDFs in private storage, downloadable only by the employee and payroll permission holders, through short-lived signed URLs.
- PAY-7 Every read of another person's compensation is audited, not only changes.

### 6.7 Private messaging (Future)

- CHAT-1 Direct and group conversations among employees.
- CHAT-2 Only current members can read a conversation. Leaving a group ends access to messages sent after leaving; access to earlier messages is a product decision to confirm.
- CHAT-3 No administrator role can read conversations through the product. An investigation access procedure (two approvers, time-bound, audited, documented in policy) is the only path, and it is designed but not built until required.
- CHAT-4 Retention period configurable; messages past retention are hard-deleted by a scheduled job.

### 6.8 Documents

- DOC-1 Documents belong to an employee (or to the company for policy documents) and to a category.
- DOC-2 Categories carry a sensitivity level that determines which permissions can read them.
- DOC-3 Files are stored in private object storage. The API authorizes each download and returns a signed URL valid for 60 seconds.
- DOC-4 Every download is audited with who, what, when, from where.
- DOC-5 Uploads are limited to an allow-list of types, verified by file content (not extension), size-limited, and scanned for malware before becoming downloadable.
- DOC-6 New uploads create a new version; old versions remain available to authorized users.
- DOC-7 Documents may carry an expiry date (e.g. visa, contract); Phase 2 sends reminders.

### 6.9 Notifications and announcements

- NTF-1 In-app notifications for items that need action or attention (approval requested, request approved, document shared).
- NTF-2 Email notifications never contain sensitive content (no salary, no leave reason, no document content). They say what happened and link to the app.
- NTF-3 Users can mute non-critical notification types. Security notifications cannot be muted.
- ANN-1 (Phase 2) HR can publish announcements to everyone or to departments/locations. Announcement text is sanitized rich text.

### 6.10 Reports and exports

- RPT-1 Reports show only data the viewer is already permitted to read. A report never grants access.
- RPT-2 Exports require the matching `*.export` permission, step-up authentication, and run as background jobs.
- RPT-3 Export files are stored privately, downloadable by the requester only, expire after 24 hours, and are protected against spreadsheet formula injection.

### 6.11 Audit and security center

- AUD-1 Audit records are written in the same database transaction as the change they describe.
- AUD-2 The application database role cannot update or delete audit records.
- AUD-3 Audit entries are sealed into a hash chain per partition, linked across partitions by signed checkpoints anchored in write-once storage. Tampering, deletion and missing partitions are detectable, including by a database superuser. Verification runs daily (`security-architecture.md` §8.1).
- AUD-4 Retention deletes only whole expired partitions, after recording a signed tombstone checkpoint, so deletion never breaks verification.
- SEC-1 Security center shows active sessions across users, failed-login patterns, lockouts, MFA resets, privilege elevations and break-glass use, and permission changes to holders of `security.event.read`.

### 6.12 Architecture versus Sigvitas policy values

The system implements **mechanisms**. Sigvitas HR supplies the **values**. No Sigvitas value is assumed in code, migrations or documentation. Test fixtures use clearly labelled test values only.

**Attendance**

| Parameter | Mechanism (architecture, fixed) | Where the value lives | Sigvitas value |
|---|---|---|---|
| Shift start / end | Shift record, versioned by effective date; crossing midnight supported; work date = shift start date | `attendance.shifts` / `shift_versions` | To be provided by HR |
| Weekly offs | Weekday set per shift; alternating patterns supported via a week-of-month rule (the pattern is an example of capability, not an assumption) | `shift_versions.weekly_off_rule` | To be provided |
| Grace period | Minutes after shift start before arrival counts as late | `attendance.attendance_policies` | To be provided |
| Late arrival | Computed as minutes past start + grace; optional rule for how many late marks convert to a half-day deduction (off unless configured) | policy | To be provided |
| Early departure | Minutes before shift end, with optional grace | policy | To be provided |
| Full-day threshold | Minimum minutes; basis selectable: effective (excluding breaks) or gross | policy | To be provided |
| Half-day threshold | Minimum minutes, same basis | policy | To be provided |
| Breaks | Maximum break minutes, whether breaks count as work, what happens when the limit is exceeded (flag only, or deduct) | policy | To be provided |
| Overtime | Enabled or disabled; basis (beyond full-day threshold or beyond shift end); minimum minutes before overtime counts; whether it needs approval | policy | To be provided |
| Midnight-crossing shifts | Supported by the mechanism; no value needed beyond the shift times | shift | — |
| Incomplete day | Minutes after shift end without clock-out before the day is marked `incomplete` | policy | To be provided |
| Multiple sessions per day | Allowed or not | policy | To be provided |

**Leave**

| Parameter | Mechanism (architecture, fixed) | Where the value lives | Sigvitas value |
|---|---|---|---|
| Leave types | Catalogue of types with code, name, paid/unpaid | `leave.leave_types` | To be provided |
| Leave year start | Month (and day) the leave year begins; may differ from the calendar year | `leave.leave_policies.leave_year_start_month` / `_day` (per policy) | To be provided |
| Accrual | Method: none, upfront per leave year, or periodic (monthly or quarterly), with an amount; proration on joining and exit | policy | To be provided |
| Opening balances | Imported as `opening_balance` ledger entries with an import reference | ledger | Provided by HR at go-live |
| Carry-forward | Cap (days or none); optional expiry of carried days | policy | To be provided |
| Lapse | What happens to the uncarried balance at year end (lapse, or keep) | policy | To be provided |
| Negative balance | Allowed or not; maximum negative days | policy | To be provided |
| Half-day support | Allowed per type | leave type | To be provided |
| Attachment requirement | Required after N consecutive days, or always, or never | leave type | To be provided |
| Day counting | Whether weekly offs and holidays inside a leave range count as leave days (per type) | leave type | To be provided |
| Approval | Reporting manager in MVP; chains in Phase 2 | architecture | — |

**Rules for implementers:** domain functions take policy objects as parameters. Policies are effective-dated and versioned; changing a value creates a new version and triggers recomputation only from its effective date. Adding a new *kind* of rule (not a new value) is a code change with an ADR. Changing a value is never a code change.

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| Security | OWASP ASVS 5.0 Level 2 as the baseline; Level 3 controls for authentication, payroll and documents. |
| Privacy | Personal data minimized, classified and access-logged. Designed to support India's Digital Personal Data Protection Act 2023 obligations (to be confirmed with Sigvitas legal). |
| Performance | p95 API latency under 300 ms for reads and 500 ms for writes at design load. Clock-in must complete in under 1 s on 4G. |
| Design load | Up to 2,000 employees and 300 concurrent users without architecture change (actual headcount to be confirmed). |
| Availability | 99.5% during business hours for MVP. Planned maintenance outside working hours. |
| Data durability | Point-in-time recovery for the database with 7-day window minimum; encrypted daily backups retained 35 days; restore tested quarterly. |
| Accessibility | WCAG 2.2 AA. |
| Devices | Latest two versions of Chrome, Edge, Firefox, Safari; iOS Safari and Android Chrome from 360 px width. |
| Time | All timestamps stored in UTC. Work dates computed in the employee's location time zone. |
| Localization | English UI at launch. Default locale, currency and week start are system settings, not code (en-IN / INR proposed, to be confirmed). Currency stored with ISO 4217 code on every amount. |

## 8. Out of scope (for now)

- Recruitment / applicant tracking.
- Performance reviews, goals, OKRs.
- Learning management.
- Expense claims and reimbursements.
- Statutory payroll computation for any country (for example Indian PF, ESI, PT, TDS) — the architecture allows it later.
- Native mobile apps — the API is designed so they can be added (see `architecture.md` §8).

## 9. Content rules

The product must not ship invented data. Seed data exists only in development and test environments, is clearly labelled as such (e.g. `dev.example` email domain, names prefixed `Test`), and the production build refuses to run seed commands. See `ui-ux-guidelines.md` §9 for writing rules.
