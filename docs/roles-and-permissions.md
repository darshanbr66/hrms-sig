# Sigvitas HRMS — Roles, permissions and account workflows

Status: reference for the implemented system (2026-10-15)
Related: `authorization-model.md` (the model and its rules), `security-architecture.md` §4 and §9, `local-development.md` §9

Sigvitas HRMS is a Human Resource Management System: employee lifecycle, organization structure, attendance and shifts, leave and holidays, employee documents, notifications, reports and exports, and access administration. It is not a recruitment platform and has no candidate, application or hiring-pipeline features.

This document says, for the code as it is today, who can do what. The matrix in §7 is generated from the code (`backend/scripts/role_matrix.py`) and a contract test fails when it drifts. The rules behind it (scopes, separation of duties, elevation) are defined in `authorization-model.md`; this document applies them per role and per workflow.

## 1. Role identifiers

Eight roles exist in code (`app/platform/authz/roles.py`) and in the database (`access.roles`, seeded by migration). Each holds an explicit list of permissions. There is no runtime role inheritance: `hr_admin` is defined in code as the `hr` set plus named additions, which produces a fixed list that the matrix shows in full, and nothing is granted by a role's position in a hierarchy.

| Business role | Identifier | How a person gets it |
|---|---|---|
| System Admin | `system_admin` | Assigned by a super admin (`role.assign`, step-up, audited). A super admin can hold it only through an approved elevation or break-glass (SOD-10). |
| Super Admin | `super_admin` | The first two are created by the one-time bootstrap command. Every later assignment needs a second super admin's approval (SOD-8). At least two must stay active. |
| HR Admin | `hr_admin` | Assigned by a super admin, optionally limited to a department or location and to an end date. |
| HR | `hr` | Assigned by a super admin, optionally limited to a department or location and to an end date. |
| Manager | `manager` | Derived, never assigned: held while the account's employee has at least one direct report on the day. |
| Employee | `employee` | Derived, never assigned: held while the account is linked to an employee record who is employed on the day. |
| (not in the six) | `payroll_admin` | Exists for Phase 2 payroll; today it holds read and export of profiles, attendance and leave only. |
| (not in the six) | `auditor` | Read-only audit log and security events, audit export. |

**Difference from the proposed six-role model:** none of the six is missing. Two points differ from what the names may suggest, and both are deliberate architecture (CLAUDE.md rule 16, `authorization-model.md` §4.2):

1. **Super Admin is not the most powerful data role.** It manages access (who holds which role, elevation approvals, break-glass) and reads the audit log and security events. It holds no employee, attendance, leave, document or payroll permission. To do HR work a super admin requests a time-bound elevation that a different super admin approves.
2. **System Admin is not an HR role.** It runs accounts, sessions, MFA resets, security settings and system settings, and holds no employee, attendance, leave, document or compensation permission.

Ordinary organization-wide HR access belongs to `hr` (day-to-day) and `hr_admin` (day-to-day plus configuration and sensitive categories).

## 2. Data tiers and the sensitive-data policy

| Tier | What it holds | Who reads it (default grants) | Controls |
|---|---|---|---|
| Directory | Name, designation, department, location, work contact of current employees | `employee` | none beyond the session |
| `internal` (profile) | Job details, reporting line, joining date, employment type, status, job and status history | self; `manager` for the team; `hr`, `hr_admin`, `payroll_admin` for everyone | `employee.profile.read.<scope>`; managers do not see HR's job-history notes |
| `personal` | Date of birth, personal contact, address, emergency contacts | self; `hr`, `hr_admin` | `employee.personal.read.all` is audited on every read; managers have none (`employee.emergency_contact.read.team` exists but no role holds it) |
| `sensitive` | Government identifiers, bank details | self, `hr_admin` (granted) | **Not built**: Sigvitas has not chosen the identifier types (roadmap §6). The permissions `employee.sensitive.*` exist, need step-up and are audited, and no route uses them yet. |
| Restricted | Compensation, payroll, payslips | nobody today | **Not built** (Phase 2, and in-house versus external payroll is undecided). No `compensation.*`, `payroll.*` or `payslip.*` permission exists in the catalog yet, so no role can reach such data. |
| Documents | Per category: `internal`, `personal`, `sensitive` | self (employee-visible categories); `manager` (team, `internal` only); `hr`, `hr_admin` (`internal`, `personal`); `hr_admin` (`sensitive`, with step-up) | Every read of someone else's documents and every download is audited; downloads only after a clean virus scan, through a 60-second link |
| Exports | CSV files produced by the worker | the requester only | `*.export` needs step-up; the rows are fixed to the requester's read scope at request time; downloadable for 24 hours |
| Leave reasons and decision notes | Free text | the employee and those who may decide the request | not in notifications or emails; reads by others are audited |

Separate permissions, as required: viewing employee records (`employee.profile.read.*`), personal details (`employee.personal.read.*`), government IDs and bank details (`employee.sensitive.read.*`, not yet used), compensation and payroll (Phase 2, not in the catalog), private documents (`document.read.*`, `document.sensitive.read.all`), bulk exports (`employee.export`, `attendance.export`, `leave.export`, `audit.export`), security administration (`security.*`, `auth.session.*.all`, `user.disable`) and emergency elevation (`role.elevation.*`).

Every one of these is decided in the service layer (`platform/authz`) for the API, the exports produced by the worker (scope fixed when the export is requested) and document downloads alike. The frontend only hides what the API would refuse.

## 3. Roles in detail

Scopes: **self** is the account's own employee record; **team** is the reporting subtree on the day; **all** is every employee, narrowed to a department or location when the assignment carries one (`authorization-model.md` §5).

### 3.1 Employee (`employee`, derived)

- **Purpose:** self-service for the person's own record.
- **May:** read the directory and organization; read and edit own self-editable profile fields and own personal details and emergency contacts; clock in and out and take breaks; read own attendance days; ask for attendance corrections and cancel them; request, preview and cancel own leave; read own balances, ledger and holidays; read shift definitions and attendance policies; read own documents in employee-visible categories and upload to categories that allow it; read company documents; own notifications and preferences.
- **Pages:** Dashboard, My attendance, My leave, Holidays, Documents, My profile, Account security, Directory, Organization, Notifications.
- **Dashboard:** clock card, upcoming holidays, my leave, recent sign-ins, shortcuts.
- **Forbidden:** anyone else's data outside the directory; deciding anything about themselves.

### 3.2 Manager (`manager`, derived)

- **Purpose:** decisions and oversight for direct and indirect reports.
- **May (team scope):** read profiles and job history (without HR notes), attendance days and team today, leave requests, balances and calendar; decide leave requests and attendance corrections of the team (never their own, SOD-1); read team documents in `internal` categories.
- **Pages:** adds Team attendance, Attendance corrections, Leave approvals, Leave calendar, Employees (team), Reports (team leave).
- **Dashboard:** adds people figures for the team, team today, leave and corrections waiting.
- **Forbidden:** personal, sensitive or compensation data of team members; exports; any account administration.

### 3.3 HR (`hr`)

- **Purpose:** day-to-day HR operations across the organization (or a department or location when the assignment says so).
- **May:** read and edit employee records and personal details (personal reads audited); create employees; job changes, status changes and the one-step exit; emergency contacts; invite accounts, link accounts to employees and disable non-administrator accounts (`user.disable` with step-up); read all attendance, decide any correction and record corrections for others (SOD-7: never for themselves); assign shifts; read all leave, decide any request, adjust balances, allocate, import opening balances and run the year end (SOD-6: never on their own balance); read and manage documents in `internal` and `personal` categories; publish company documents; attendance and leave exports (step-up).
- **Pages:** Employees, Organization (read), Leave overview, Leave balances, Reports, Attendance set-up (assignments), Users, plus the team pages at organization scope.
- **Forbidden:** organization, shift, policy, leave-type, holiday and document-category configuration; `sensitive` document categories; employee export; role assignment; administrator accounts; security settings; compensation and payroll.

### 3.4 HR Admin (`hr_admin`)

- **Purpose:** everything HR does, plus HR configuration.
- **May, in addition to HR:** manage locations, departments, designations and department heads; define shifts, location default shifts and attendance policies; lock attendance periods; define leave types and policies; maintain holiday calendars; define document categories; read `sensitive` document categories (step-up, audited); employee export (step-up); read roles and settings.
- **Pages:** adds Holiday calendars, Leave types, Attendance set-up (full), Document categories, Roles, Settings (read).
- **Forbidden:** role assignment, elevation, administrator accounts, security and system settings, the audit log, compensation and payroll.

### 3.5 System Admin (`system_admin`)

- **Purpose:** operate the accounts and the platform.
- **May:** list accounts; invite accounts; disable and re-enable accounts, including administrator accounts (only a system admin may, and at least two super admins stay active); reset another user's MFA (step-up, never their own, SOD-4); read and revoke anyone's sessions (revoking needs step-up); read security events; change security and system settings (step-up); read roles and settings.
- **Pages:** Users, Roles, Access requests (read), Security events, Settings.
- **Forbidden:** every employee, attendance, leave, document and compensation permission; role assignment; the audit log.

### 3.6 Super Admin (`super_admin`)

- **Purpose:** access governance.
- **May:** list accounts; read roles and who holds them; assign and remove roles for others (step-up, never their own, SOD-3); request a time-bound elevation for themselves (step-up, reason); approve, reject, acknowledge and revoke other super admins' requests (step-up, SOD-9); break-glass `system_admin` for up to one hour when no approver is available (step-up, reason, every super admin and auditor notified, a review item another super admin must acknowledge); read the audit log, security events and settings.
- **Pages:** Users, Roles, Access requests, Security events, Audit log, Settings (read).
- **Dashboard:** access requests waiting, a panel explaining how to get time-bound HR access, recent sign-ins.
- **Forbidden:** any data permission held directly; standing assignments of `hr`, `hr_admin`, `payroll_admin`, `system_admin` or `auditor` (SOD-10); assigning `super_admin` without a second super admin's approval (SOD-8); elevating to `super_admin`.

### 3.7 Payroll admin and auditor

`payroll_admin` reads profiles, attendance and leave organization-wide and exports attendance and leave; compensation and payroll permissions arrive with Phase 2. `auditor` reads sessions of all users, security events, roles and the audit log, and exports the audit log (step-up).

## 4. Step-up, reasons, approvals and audit

- **Step-up** (MFA again within 10 minutes) is needed for every permission flagged SU in §7: exports, role assignment, every elevation action, user disable, session revocation of others, MFA reset, security and system settings, sensitive identifiers and `sensitive` document categories, and linking an account to an employee.
- **Reasons** are required for elevation and break-glass requests, status changes and exits, leave balance adjustments, opening balances, rejections of leave requests and corrections, and HR-recorded attendance corrections.
- **Approvals:** super admin assignment (SOD-8), elevation (SOD-9), leave requests and attendance corrections (SOD-1), all by someone other than the subject.
- **Audit:** every change is written to the audit log in the same transaction; permissions flagged R are audited on reads too; sensitive values are referenced, never copied; actions taken while elevated carry the elevation ID.

## 5. Elevation, expiry and break-glass

1. A super admin requests a role (`hr`, `hr_admin`, `payroll_admin`, `system_admin` or `auditor`) for 2 to 8 hours with a reason and an optional department or location limit (step-up).
2. A **different** super admin approves or rejects it (step-up). Requests not decided in 24 hours expire.
3. Approval creates a role assignment with an end time. Permissions are resolved on every request, so it stops applying at the end time with no job involved. Either super admin can end it early.
4. Break-glass gives the requester `system_admin` alone for at most one hour, with no approver, and notifies every super admin and auditor; another super admin must acknowledge it afterwards.

## 6. Account and employee lifecycle workflows

A user account (sign-in identity) and an employee record (HR data) are separate. An account may have no employee record (for example the super admins), and an employee record may have no account (for example someone who never signs in). Linking is explicit and audited.

| # | Workflow | How it works today |
|---|---|---|
| 1 | Initial bootstrap | Run `python -m app.cli bootstrap-super-admins --email A --email B` once per database. It invites two `super_admin` accounts together (SOD-8 needs two). It refuses to run again. |
| 2 | First administrator | Open the invite link from the email (Mailpit locally), set a password, scan the QR code into an authenticator app, confirm a code, save the recovery codes. The account is active only after MFA is enrolled. |
| 3 | Second administrator | Invited by the same bootstrap command; activates the same way. Later super admins: a super admin assigns `super_admin` to an existing account, and a second super admin approves (SOD-8). |
| 4 | HR admins and HR users | An account holding `user.invite` (`system_admin`, `hr`, `hr_admin`; a super admin through elevation to `system_admin`) invites the email. After activation, a super admin assigns `hr` or `hr_admin` (step-up, audited). An invite grants no role. |
| 5 | Employees | HR creates the employee record with a work email, then invites an account linked to that record (`user.invite` plus `employee.lifecycle.manage` over the employee). The invite email must equal the work email. |
| 6 | Employee record without access | Supported: create the record and do not invite. Attendance, leave and documents can still be kept for that person by HR. |
| 7 | Link an existing account | `POST /users/{id}/employee-link` (Employee record → HRMS account → Link an existing account): `user.invite` plus `employee.lifecycle.manage` over the employee, step-up, a reason. Refused for one's own account, administrator accounts and disabled accounts; one account per employee. |
| 8 | Email match | The account email must equal the employee's work email, at invite and at linking. |
| 9 | Activation and MFA | Invite link (72 hours, single use) → password (checked against common and breached passwords) → TOTP enrolment within 30 minutes → recovery codes. There is no password-only path. |
| 10 | Role changes | Super admin only (`role.assign`, step-up, reason recorded in audit), never for themselves. `employee` and `manager` cannot be assigned. `super_admin` needs a second super admin's approval. |
| 11 | Temporary elevation | §5. |
| 12 | Disabling and sessions | `user.disable` (step-up) disables the account and at once revokes every session, trusted device and outstanding invite. Administrator accounts only by a system admin. Each person can revoke their own sessions; a system admin can revoke anyone's. |
| 13 | MFA and account recovery | Lost authenticator: sign in with a recovery code, then enrol a new factor. No recovery codes left: a system admin resets MFA (step-up, never their own); the person gets a re-enrolment link by email. Forgotten password: the public reset link by email; signing in afterwards still needs MFA. The last MFA factor can never be removed. |
| 14 | Exit | Employee record → status → Exit (`employee.lifecycle.manage`, reason): the record becomes exited, and in the same transaction the linked account is disabled and its sessions revoked (needs `user.disable` and step-up). An administrator account is left for a system admin to disable, and the result says so. History is kept. |

Self-approval, self-granting of roles, linking to someone else's work email and unaudited administrative changes are refused by the service layer and covered by tests (`tests/integration/test_authorization_matrix.py`, `test_grant_requests.py`, `test_admin_account_disable.py`, `test_account_links.py`).

Verification of these workflows (2026-10-16, checked against the code):

| Workflow | Implemented | Tested end to end over HTTP | Browser-verified |
|---|---|---|---|
| 4 HR and HR admin accounts | Yes | Yes: `test_staff_provisioning_journey.py` (a super admin cannot invite; a system admin invites but cannot assign roles; activation with a password and an authenticator; a super admin assigns `hr_admin` and `hr` only after step-up, with the reason in the audit log; an HR admin holds `org.manage` and `user.invite` but not `role.assign` and cannot change anyone's roles, their own included; `hr` cannot manage the organization) | No |
| 5 Employee invited and linked | Yes | Yes: `test_onboarding_journey.py` | Not by engineering. The `hrms_local_test` audit log shows the owner created an employee, invited and activated a linked account on 2026-10-16 |
| 6 Record without access | Yes | Yes: `test_people_api.py` | No |
| 9 Activation and MFA | Yes | Yes: both journeys and `test_invitations.py` (the password step never yields a session) | As for 5 |
| 12 Disabling and sessions | Yes | Yes: the staff journey (step-up required; the disabled account's session and refresh stop at once) and `test_admin_account_disable.py` | No |
| 13 Recovery | Yes | Yes: `test_mfa_reset.py`, `test_password_reset.py`, `test_auth_account.py` | No |

What a super admin can do toward these workflows: invite nobody directly (no `user.invite`); assign `hr`, `hr_admin`, `payroll_admin`, `system_admin` and `auditor` to other accounts (step-up, reason); request `super_admin` for another account, approved by a second super admin. To invite, a super admin either asks a `system_admin` or HR account, or obtains a time-bound elevation to `system_admin` or `hr_admin` approved by the other super admin (§5). An elevated super admin still cannot assign a role to themselves.

## 7. Permission matrix (generated)

`SU` needs step-up. `R` audited on every use, reads included. "Every account" is the account baseline every active account holds, linked or not.

<!-- role-matrix:start (generated by `uv run python -m scripts.role_matrix`; do not edit) -->

| Permission | Step-up | Audited | Every account | `employee` | `manager` | `hr` | `hr_admin` | `payroll_admin` | `system_admin` | `super_admin` | `auditor` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `auth.session.read.self` |  |  | ● |  |  |  |  |  |  |  |  |
| `auth.session.revoke.self` |  |  | ● |  |  |  |  |  |  |  |  |
| `auth.session.read.all` |  | R |  |  |  |  |  |  | ● |  | ● |
| `auth.session.revoke.all` | SU | R |  |  |  |  |  |  | ● |  |  |
| `user.read.all` |  |  |  |  |  | ● | ● |  | ● | ● |  |
| `user.invite` |  | R |  |  |  | ● | ● |  | ● |  |  |
| `user.disable` | SU | R |  |  |  | ● | ● |  | ● |  |  |
| `security.mfa.reset` | SU | R |  |  |  |  |  |  | ● |  |  |
| `security.event.read` |  | R |  |  |  |  |  |  | ● | ● | ● |
| `security.settings.manage` | SU | R |  |  |  |  |  |  | ● |  |  |
| `role.read` |  |  |  |  |  |  | ● |  | ● | ● | ● |
| `role.assign` | SU | R |  |  |  |  |  |  |  | ● |  |
| `role.elevation.request` | SU | R |  |  |  |  |  |  |  | ● |  |
| `role.elevation.approve` | SU | R |  |  |  |  |  |  |  | ● |  |
| `role.elevation.break_glass` | SU | R |  |  |  |  |  |  |  | ● |  |
| `audit.read` |  | R |  |  |  |  |  |  |  | ● | ● |
| `audit.export` | SU | R |  |  |  |  |  |  |  |  | ● |
| `settings.read` |  |  |  |  |  |  | ● |  | ● | ● |  |
| `settings.manage` | SU | R |  |  |  |  |  |  | ● |  |  |
| `org.read` |  |  |  | ● |  | ● | ● |  |  |  |  |
| `org.manage` |  | R |  |  |  |  | ● |  |  |  |  |
| `employee.directory.read` |  |  |  | ● |  |  |  |  |  |  |  |
| `employee.profile.read.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `employee.profile.read.team` |  |  |  |  | ● |  |  |  |  |  |  |
| `employee.profile.read.all` |  |  |  |  |  | ● | ● | ● |  |  |  |
| `employee.profile.update.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `employee.profile.update.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `employee.personal.read.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `employee.personal.read.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `employee.personal.update.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `employee.personal.update.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `employee.emergency_contact.read.team` |  | R |  |  |  |  |  |  |  |  |  |
| `employee.sensitive.read.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `employee.sensitive.read.all` | SU | R |  |  |  |  | ● |  |  |  |  |
| `employee.sensitive.update.all` | SU | R |  |  |  |  | ● |  |  |  |  |
| `employee.create` |  | R |  |  |  | ● | ● |  |  |  |  |
| `employee.lifecycle.manage` |  | R |  |  |  | ● | ● |  |  |  |  |
| `employee.export` | SU | R |  |  |  |  | ● |  |  |  |  |
| `attendance.clock.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `attendance.read.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `attendance.read.team` |  |  |  |  | ● |  |  |  |  |  |  |
| `attendance.read.all` |  |  |  |  |  | ● | ● | ● |  |  |  |
| `attendance.correction.request.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `attendance.correction.approve.team` |  |  |  |  | ● |  |  |  |  |  |  |
| `attendance.correction.approve.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `attendance.correction.create.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `attendance.export` | SU | R |  |  |  | ● | ● | ● |  |  |  |
| `shift.read` |  |  |  | ● |  | ● | ● |  |  |  |  |
| `shift.manage` |  | R |  |  |  |  | ● |  |  |  |  |
| `attendance.policy.manage` |  | R |  |  |  |  | ● |  |  |  |  |
| `shift.assign.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `leave.request.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `leave.read.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `leave.read.team` |  |  |  |  | ● |  |  |  |  |  |  |
| `leave.read.all` |  |  |  |  |  | ● | ● | ● |  |  |  |
| `leave.request.approve.team` |  |  |  |  | ● |  |  |  |  |  |  |
| `leave.request.approve.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `leave.balance.adjust` |  | R |  |  |  | ● | ● |  |  |  |  |
| `leave.policy.manage` |  | R |  |  |  |  | ● |  |  |  |  |
| `leave.export` | SU | R |  |  |  | ● | ● | ● |  |  |  |
| `holiday.read` |  |  |  | ● |  | ● | ● |  |  |  |  |
| `holiday.manage` |  | R |  |  |  |  | ● |  |  |  |  |
| `document.read.self` |  | R |  | ● |  |  |  |  |  |  |  |
| `document.upload.self` |  |  |  | ● |  |  |  |  |  |  |  |
| `document.read.team` |  | R |  |  | ● |  |  |  |  |  |  |
| `document.read.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `document.sensitive.read.all` | SU | R |  |  |  |  | ● |  |  |  |  |
| `document.manage.all` |  | R |  |  |  | ● | ● |  |  |  |  |
| `document.category.manage` |  | R |  |  |  |  | ● |  |  |  |  |
| `document.company.read` |  |  |  | ● |  |  |  |  |  |  |  |
| `document.company.manage` |  | R |  |  |  | ● | ● |  |  |  |  |
| `notification.read.self` |  |  | ● | ● |  |  |  |  |  |  |  |
| `notification.preference.update.self` |  |  | ● | ● |  |  |  |  |  |  |  |

<!-- role-matrix:end -->

Coverage by area, for reading the matrix: employees and history (`employee.*`), organization (`org.*`), attendance and corrections (`attendance.*`), shifts and policies (`shift.*`, `attendance.policy.manage`), leave (`leave.*`), holidays (`holiday.*`), documents (`document.*`), notifications (`notification.*`), reports and exports (`*.export`; reports have no permission of their own and show only what the reader can already read), users and invitations (`user.*`), linking (`user.invite` plus `employee.lifecycle.manage`), roles (`role.*`), settings (`settings.*`, `security.settings.manage`), security events (`security.event.read`), audit (`audit.*`), sessions and MFA (`auth.session.*`, `security.mfa.reset`), emergency access (`role.elevation.*`). Payroll and compensation have no permissions yet (§2).

## 8. Decisions still needed from Sigvitas

- Confirm the default grants in §7 (roadmap §6). Changing them is a data migration.
- Who the super admins are, knowing they get no standing HR data access.
- Whether managers may see team emergency contacts (`employee.emergency_contact.read.team`, held by no role today).
- Identifier types and bank details, which unblock the `sensitive` tier.
- In-house versus external payroll, which decides the compensation and payroll scope.
