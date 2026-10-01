# Sigvitas HRMS — Authorization Model

Status: Draft v0.1
Related: `security-architecture.md` §4, `database-design.md` (`access` schema), `api-architecture.md` §6

## 1. Model in one paragraph

Users hold **roles**. Roles are named bundles of **permissions**. A permission names a **domain, resource, action and scope** (`leave.request.approve.team`). A role **assignment** may be constrained to a department or location, which narrows `all` scope. At request time the engine combines the actor's permissions with **relationships** (am I this employee? am I in their reporting chain?), **resource state** (is this request still pending? is this period locked?) and **separation-of-duties rules** (is this my own request?) to decide. Some permissions additionally require **step-up** authentication. Effective permissions are resolved from PostgreSQL on every request, with no cache, so revocations and expiries apply immediately. This is RBAC for capability plus relationship- and attribute-based checks for data, all evaluated on the server.

## 2. Permission naming

```
<domain>.<resource>.<action>[.<scope>]
```

- `domain` — module (`employee`, `attendance`, `leave`, `compensation`, …).
- `resource` — thing within the module (`profile`, `personal`, `correction`, `request`, …). Omitted when the domain has one main resource.
- `action` — `read`, `create`, `update`, `delete`, `approve`, `export`, `manage`, or a domain verb (`clock`, `finalize`, `publish`).
- `scope` — `self`, `team`, `all`. Omitted for actions that are not about a person's data (e.g. `org.manage`).

Scopes:

| Scope | Meaning |
|---|---|
| `self` | Records where the subject employee is the actor's own employee record. |
| `team` | Records of employees in the actor's reporting subtree (direct and indirect reports), as of now, plus historical records from periods when the actor was in that employee's reporting chain. |
| `all` | All employees, narrowed by the role assignment's department/location constraint if set. |

A higher scope does not imply a lower one automatically in the catalog; the engine treats `all` ⊇ `team` ⊇ `self` when evaluating, but roles list what they need explicitly for readability.

## 3. Permission catalog

`SU` = requires step-up (10 min). `R` = every use is audited including reads. `SoD` = separation-of-duties rule applies.

### 3.1 Identity, access, security

| Permission | Description | Flags |
|---|---|---|
| `auth.session.read.self` | See own sessions and login history | |
| `auth.session.revoke.self` | Revoke own sessions | |
| `auth.session.read.all` | See sessions of all users | R |
| `auth.session.revoke.all` | Revoke any user's sessions | SU, R |
| `user.read.all` | List user accounts and account state | |
| `user.invite` | Create accounts / resend invites for employees | R |
| `user.disable` | Disable / re-enable accounts | SU, R, SoD (not self) |
| `security.mfa.reset` | Reset another user's MFA | SU, R, SoD (not self) |
| `security.event.read` | Read security events and login history of all users | R |
| `security.settings.manage` | Password, session, lockout, IP policies | SU, R |
| `role.read` | View roles and their permissions, and who holds them | |
| `role.manage` | Create/edit custom roles (Phase 2) | SU, R |
| `role.assign` | Assign/remove roles for other users. Assigning `super_admin` additionally requires approval by a second super admin (SOD-8). | SU, R, SoD (not self) |
| `role.elevation.request` | Request a time-bound role for oneself (§4.3) | SU, R |
| `role.elevation.approve` | Approve or reject another super admin's elevation request | SU, R, SoD (not own request) |
| `role.elevation.break_glass` | Self-activate `system_admin` for up to 1 hour when no approver is available (§4.3) | SU, R, notifies all super admins and auditors |
| `audit.read` | Read audit log (values of restricted data are never in it) | R |
| `audit.export` | Export audit log | SU, R |
| `settings.read` | Read system settings | |
| `settings.manage` | Change system settings | SU, R |

### 3.2 Organization and people

| Permission | Description | Flags |
|---|---|---|
| `org.read` | Departments, designations, locations | |
| `org.manage` | Create/update/archive departments, designations, locations | R |
| `employee.directory.read` | Directory fields of active employees | |
| `employee.profile.read.self` / `.team` / `.all` | `internal` tier: job details, reporting line, joining date, employment type, status | |
| `employee.profile.update.self` | Self-editable fields only (preferred name, photo, pronouns if enabled) | |
| `employee.profile.update.all` | HR edits of profile fields | R |
| `employee.personal.read.self` / `.all` | `personal` tier: DOB, personal contact, address, emergency contacts | `.all`: R |
| `employee.personal.update.self` | Own personal details | |
| `employee.personal.update.all` | HR edits of personal details | R |
| `employee.emergency_contact.read.team` | Emergency contacts of team (for managers, off by default) | R |
| `employee.sensitive.read.self` | Own government IDs and bank details (masked by default, full on step-up) | SU for unmasked |
| `employee.sensitive.read.all` | Others' sensitive identifiers | SU, R |
| `employee.sensitive.update.all` | Edit sensitive identifiers | SU, R |
| `employee.create` | Create employee records | R |
| `employee.lifecycle.manage` | Job changes (transfer, promotion, manager change), status changes, exit | R |
| `employee.export` | Export employee data | SU, R |

### 3.3 Attendance and shifts

| Permission | Description | Flags |
|---|---|---|
| `attendance.clock.self` | Clock in/out, start/end break | |
| `attendance.read.self` / `.team` / `.all` | Events and daily summaries | |
| `attendance.correction.request.self` | Submit correction for own attendance | |
| `attendance.correction.approve.team` | Decide corrections for team | SoD |
| `attendance.correction.approve.all` | Decide any correction, including locked periods | R, SoD |
| `attendance.correction.create.all` | HR-initiated correction on behalf of an employee (auto-approved by a second person or recorded with reason, see §6) | R, SoD |
| `attendance.export` | Export attendance | SU, R |
| `shift.read` | View shift definitions | |
| `shift.manage` | Define shifts | R |
| `attendance.policy.manage` | Define effective-dated attendance policies (grace, thresholds, overtime, breaks) | R |
| `shift.assign.all` | Assign shifts to employees | R |

### 3.4 Leave and holidays

| Permission | Description | Flags |
|---|---|---|
| `leave.request.self` | Apply for, cancel own leave | |
| `leave.read.self` / `.team` / `.all` | Requests and balances | |
| `leave.request.approve.team` | Decide team requests | SoD |
| `leave.request.approve.all` | Decide any request | R, SoD |
| `leave.balance.adjust` | Manual ledger adjustments with reason | R, SoD (not self) |
| `leave.policy.manage` | Leave types, accrual rules | R |
| `leave.export` | Export leave data | SU, R |
| `leave.calendar.read.department` | Phase 2, optional: see who in own department is away (dates only, no type or reason) | |
| `holiday.read` | View holiday calendars | |
| `holiday.manage` | Maintain holiday calendars | R |

Team members' leave reasons are `personal`: managers deciding a request see the reason for that request; the team calendar shows only "on leave" and the leave type.

### 3.5 Compensation and payroll (Phase 2)

| Permission | Description | Flags |
|---|---|---|
| `compensation.read.self` | Own current and past compensation | |
| `compensation.read.team` | Team compensation. **Not granted to any default role.** Exists so Sigvitas can decide to grant it to specific managers. | SU, R |
| `compensation.read.all` | All compensation | SU, R |
| `compensation.update` | Create compensation revisions | SU, R, SoD (not self) |
| `compensation.export` | Export compensation | SU, R |
| `payroll.component.manage` | Salary components and structures | SU, R |
| `payroll.period.read` | View payroll periods and totals | SU, R |
| `payroll.period.manage` | Create periods, prepare, move to review | SU, R |
| `payroll.period.finalize` | Finalize a period (four-eyes) | SU, R, SoD (not the preparer) |
| `payroll.period.publish` | Publish payslips to employees | SU, R |
| `payslip.read.self` | Own payslips | R |
| `payslip.read.all` | Any payslip | SU, R |

### 3.6 Documents

Document permissions combine with the category's sensitivity level. A category is `internal`, `personal` or `sensitive`; payslips live in their own restricted path and are governed by `payslip.*`.

| Permission | Description | Flags |
|---|---|---|
| `document.read.self` | Own documents in categories marked employee-visible | R (downloads) |
| `document.upload.self` | Upload own documents to self-upload categories | |
| `document.read.team` | Team documents in `internal` categories only | R |
| `document.read.all` | All documents in `internal` and `personal` categories | R |
| `document.sensitive.read.all` | Documents in `sensitive` categories (medical, ID proofs) | SU, R |
| `document.manage.all` | Upload, replace, archive documents for any employee | R |
| `document.category.manage` | Define categories and their sensitivity | R |
| `document.company.read` | Company-wide documents (policies) | |
| `document.company.manage` | Publish company-wide documents | R |

### 3.7 Communication, requests, assets, lifecycle, reports, search

| Permission | Description | Phase |
|---|---|---|
| `notification.read.self` | Own notifications | MVP |
| `notification.preference.update.self` | Own preferences | MVP |
| `announcement.read` | Read announcements targeted at the user | Phase 2 |
| `announcement.publish` | Publish announcements | Phase 2 |
| `request.submit.self` | Raise HR requests | Phase 2 |
| `request.read.self` / `.team` / `.all` | See requests | Phase 2 |
| `request.process.all` | Work on / resolve requests | Phase 2 |
| `request.type.manage` | Configure request types | Phase 2 |
| `asset.read.self` / `.all` | Assets assigned | Phase 2 |
| `asset.manage` | Manage assets and assignments | Phase 2 |
| `onboarding.manage` / `offboarding.manage` | Checklists and tasks | Phase 2 |
| `lifecycle.task.complete` | Complete tasks assigned to the user (no scope; assignment is the authorization) | Phase 2 |
| `chat.use` | Direct and group messaging | Future |
| `chat.retention.manage` | Retention settings (no content access) | Future |
| `ai.assistant.use` | Use the assistant (the assistant's data access = the user's) | Future |

**Reports and search do not have their own data permissions.** A report or search result includes only rows and fields the actor can already read. Exports require the domain's `*.export` permission. This prevents "reports" from becoming an unaudited side door.

## 4. Roles

System roles are defined in code (seeded by migration) and cannot be edited through the UI. Phase 2 adds custom roles, which cannot include `role.*`, `security.*` or `settings.manage`.

| Role key | Assigned how | Purpose |
|---|---|---|
| `employee` | Automatically to every active user linked to an employee record | Self-service |
| `manager` | **Derived**: held automatically while the user has at least one active direct report. Never assigned manually. | Team scope |
| `hr` | Manual | Day-to-day HR operations |
| `hr_admin` | Manual | HR configuration and policies |
| `payroll_admin` | Manual (Phase 2) | Compensation and payroll |
| `system_admin` | Manual | Accounts, sessions, settings, security events |
| `super_admin` | Manual, two to three holders; assignment needs a second super admin's approval | Role and permission management, controlled elevation, break-glass recovery. **No data access** (§4.2) |
| `auditor` | Manual, optional | Read-only audit and security events for compliance review |

Deriving `manager` from reporting lines removes a common failure: a manager is moved and keeps team access because nobody removed the role.

### 4.1 Default role → permission matrix

`●` granted. Scope shown where relevant.

| Permission | employee | manager | hr | hr_admin | payroll_admin | system_admin | super_admin | auditor |
|---|---|---|---|---|---|---|---|---|
| auth.session.read/revoke.self | (account baseline: every active account) | | | | | | | |
| auth.session.read.all | | | | | | ● | | ● |
| auth.session.revoke.all | | | | | | ● | | |
| user.read.all | | | ● | ● | | ● | ● | |
| user.invite | | | ● | ● | | ● | | |
| user.disable | | | ● | ● | | ● | | |
| security.mfa.reset | | | | | | ● | | |
| security.event.read | | | | | | ● | ● | ● |
| security.settings.manage | | | | | | ● | | |
| role.read | | | | ● | | ● | ● | ● |
| role.manage / role.assign | | | | | | | ● | |
| role.elevation.request / approve / break_glass | | | | | | | ● | |
| audit.read | | | | | | | ● | ● |
| audit.export | | | | | | | | ● |
| settings.read | | | | ● | | ● | ● | |
| settings.manage | | | | | | ● | | |
| org.read | ● | | | | | | | |
| org.manage | | | | ● | | | | |
| employee.directory.read | ● | | | | | | | |
| employee.profile.read | self | team | all | all | all | | | |
| employee.profile.update.self | ● | | | | | | | |
| employee.profile.update.all | | | ● | ● | | | | |
| employee.personal.read / update | self | | all | all | | | | |
| employee.emergency_contact.read.team | | (optional) | | | | | | |
| employee.sensitive.read | self | | | all | all (bank only, Phase 2) | | | |
| employee.sensitive.update.all | | | | ● | | | | |
| employee.create | | | ● | ● | | | | |
| employee.lifecycle.manage | | | ● | ● | | | | |
| employee.export | | | | ● | | | | |
| attendance.clock.self | ● | | | | | | | |
| attendance.read | self | team | all | all | all | | | |
| attendance.correction.request.self | ● | | | | | | | |
| attendance.correction.approve | | team | all | all | | | | |
| attendance.correction.create.all | | | ● | ● | | | | |
| attendance.export | | | ● | ● | ● | | | |
| shift.read | ● | | | | | | | |
| shift.manage | | | | ● | | | | |
| attendance.policy.manage | | | | ● | | | | |
| shift.assign.all | | | ● | ● | | | | |
| leave.request.self | ● | | | | | | | |
| leave.read | self | team | all | all | all | | | |
| leave.request.approve | | team | all | all | | | | |
| leave.balance.adjust | | | ● | ● | | | | |
| leave.policy.manage | | | | ● | | | | |
| leave.export | | | ● | ● | ● | | | |
| holiday.read | ● | | | | | | | |
| holiday.manage | | | | ● | | | | |
| compensation.read | self | | | | all | | | |
| compensation.update / export | | | | | ● | | | |
| payroll.* | | | | | ● | | | |
| payslip.read | self | | | | all | | | |
| document.read | self | team | all | all | | | | |
| document.upload.self | ● | | | | | | | |
| document.sensitive.read.all | | | | ● | | | | |
| document.manage.all | | | ● | ● | | | | |
| document.category.manage | | | | ● | | | | |
| document.company.read | ● | | | | | | | |
| document.company.manage | | | ● | ● | | | | |
| notification.* (self) | ● | | | | | | | |

Roles are additive: an HR person is also an `employee` and possibly a `manager`. Every active account also holds the **account baseline** (`auth.session.read.self`, `auth.session.revoke.self`: its own sessions and sign-in history), whether or not it is linked to an employee record, because accounts such as the bootstrapped super admins have none. The account's own sign-in security (`/me`, password, MFA factors, recovery codes, step-up, sign-out) needs no permission: those routes are on an explicit account allow-list. The derived `employee` role applies while the linked employee is employed on the day (joined, not past the exit date), the same rule team resolution uses. Notably:

- `system_admin` has **no** employee data, document, or compensation access.
- `super_admin` has **no** compensation, payroll, personal, sensitive, document or message access (§4.2).
- `hr` and `hr_admin` have **no** compensation access unless also `payroll_admin`.
- `manager` has **no** personal, sensitive or compensation access to team members.

The least-privilege boundaries above are fixed architecture. The exact grants in the matrix are a proposed default for Sigvitas HR to confirm before M2 (see roadmap §6). Changing them is a data migration, not a code change.

### 4.2 Super admin: definition

`super_admin` exists to manage access, not to use it.

| Holds | Does not hold |
|---|---|
| `role.read`, `role.manage`, `role.assign` | `compensation.*`, `payroll.*`, `payslip.read.all` |
| `role.elevation.request`, `role.elevation.approve`, `role.elevation.break_glass` | `employee.personal.*.all`, `employee.sensitive.*`, `employee.export` |
| `user.read.all`, `security.event.read`, `audit.read`, `settings.read` | `document.*` beyond the self-service defaults every employee has |
| | Any message content access. No such permission exists in the catalog. |

A super admin is usually also an employee and holds the `employee` role for their own data, like everyone else.

### 4.3 Privilege elevation

Elevation is the only way a super admin obtains a role that grants data access. It is stored in `access.role_elevations` (`database-design.md` §4.2).

1. **Request** (`role.elevation.request`): role, reason (required, free text), duration (default 2 h, maximum 8 h), optional department/location constraint. Requires step-up. All super admins are notified.
2. **Approve or reject** (`role.elevation.approve`): by a **different** super admin, with step-up. Requests not decided within 24 hours expire.
3. **Activate:** approval creates an `access.user_roles` row with `valid_until` set and linked to the elevation. The requester's sessions are rotated. All super admins are notified.
4. **Expire or revoke:** the assignment stops applying at `valid_until`, because permissions are resolved per request. The requester or any other super admin can revoke it early. Both are audited and notified.
5. **Every action taken while elevated** carries the elevation ID in its audit record, so an elevated session can be reviewed as a whole.

**Break-glass** (`role.elevation.break_glass`) is the exception for emergencies when no second super admin can approve. It can self-activate only `system_admin` (account recovery: unlock, MFA reset for administrators, session revocation), for at most 1 hour, with step-up and a mandatory reason. It notifies all super admins and auditors at high severity, and it creates a review item that another super admin must acknowledge. Break-glass cannot reach payroll, sensitive data, documents or messages.

Elevation to `super_admin` itself is not possible; assigning `super_admin` goes through `role.assign` with SOD-8.

**Bootstrap:** the one-time installation CLI invites the first **two** super admins together, because SOD-8 cannot be satisfied with fewer than two. SOD-8 applies to every assignment after that.

**No standing data roles for super admins** (SOD-10): a user who holds `super_admin` cannot also hold a permanent assignment of `hr`, `hr_admin`, `payroll_admin`, `system_admin` or `auditor`. They get those only through time-bound elevation. If a person needs one of those roles for daily work, they should not be a super admin. Sigvitas should pick super admins with this in mind (roadmap §6).

## 5. Role assignment constraints

`access.user_roles` rows carry optional `department_id` and `location_id`. When set, `all` scope for that role means "employees currently in that department/location": employed on the date and placed there by the job row that applies on it. Example: an HR person for one office. An employee with no applying job row is outside every restricted scope. Several assignments of a role combine (the union of their constraints).

Assignments also carry `valid_from` / `valid_until` for temporary access (e.g. covering HR during leave). Expired assignments stop applying without a manual step.

## 6. Separation-of-duties rules

Enforced in `platform/authz` regardless of permissions held:

| Rule | Applies to |
|---|---|
| SOD-1 Actor cannot decide a request where they are the subject | leave, attendance corrections, HR requests, profile change approvals |
| SOD-2 Actor cannot change their own compensation | `compensation.update` |
| SOD-3 Actor cannot assign or remove their own roles | `role.assign` |
| SOD-4 Actor cannot reset their own MFA or disable their own account via admin endpoints | `security.mfa.reset`, `user.disable` |
| SOD-5 Payroll period finalizer must differ from the person who moved it to review | `payroll.period.finalize` |
| SOD-6 Actor cannot adjust their own leave balance | `leave.balance.adjust` |
| SOD-7 HR-initiated attendance corrections for an employee are applied immediately but notify the employee and their manager; corrections to the actor's own attendance always go through normal approval | `attendance.correction.create.all` |
| SOD-8 Assigning the `super_admin` role requires approval by a second super admin (not the assigner, not the assignee) | `role.assign` for `super_admin` |
| SOD-9 An elevation request cannot be approved by its requester; break-glass is limited to `system_admin` for 1 hour | `role.elevation.*` |
| SOD-10 A `super_admin` holder cannot hold standing (non-elevated) `hr`, `hr_admin`, `payroll_admin`, `system_admin` or `auditor` assignments, and those roles cannot be assigned to a user while they hold `super_admin` | `role.assign` |

When the approver for a request is also its subject (e.g. a manager with no manager), the request routes to `hr` holders with `.approve.all`.

## 7. Evaluation

```python
# service layer, illustrative
actor = ctx.actor                        # user, employee_id, permissions, step_up_at
authz.require(actor, "leave.request.approve", resource=leave_request)
```

The engine:

1. Finds all actor permissions matching `leave.request.approve.*`.
2. For each scope held, checks whether the resource's subject employee falls in it (`self` → equal; `team` → in reporting subtree at the relevant time; `all` → within assignment constraint).
3. Applies SoD rules and resource state rules from the module's `policies.py`.
4. Checks step-up if the permission is flagged.
5. Records an audit entry if the permission is flagged `R` (success) or the decision is a denial on a sensitive resource.

For lists:

```python
filter_ = authz.scope_filter(actor, "attendance.read")   # SQL expression
rows = repo.list_days(filter_, date_range, page)
```

`scope_filter` returns the union of all scopes the actor holds, expressed as a SQL predicate on `employee_id`. If the actor holds none, it raises 403 before any query runs.

Implementation (`platform/authz`): `requires(permission)` on the route is the fast fail (held at some scope) and records a denial of an R-flagged permission as an `access.denied_sensitive` security event, in its own short transaction before the route opens one. The service calls `Authorizer.require(...)` for the full decision: the narrowest held scope that covers the subject wins and is the `permission_used` in the audit row; step-up is checked only after the permission, so a user without it learns nothing about step-up. Permissions not about a person's data (`user.disable`, `auth.session.revoke.all`) are decided by their exact key. The engine reaches reporting lines and placements through a `Relationships` protocol that the people module implements, so the platform imports no module. "Today" is the UTC date in M1; per-location dates arrive with location time zones (M2), and until then a change effective on a date applies from UTC midnight.

**Team resolution.** The reporting subtree is computed with a recursive CTE over the job records that apply on a date (`people.employee_jobs` where `effective_from <= d < effective_to`), counting only employees employed on that date. Open-ended rows are not the same thing: a future-dated transfer closes the current row and opens the future one, so selecting `effective_to IS NULL` would move team access before the transfer takes effect. "Now" passes today's date; a historical record passes its own date, which gives the "team at the time of the record" rule in §2. The people module exposes this as `team_member_ids_query` (for scope filters), `is_team_member` (single record, walking up the chain) and `has_direct_reports` (derived `manager` role). At Sigvitas's expected size this is milliseconds. If it becomes a hotspot, a materialized closure table refreshed on job changes replaces it without changing the engine's interface.

## 8. Error semantics

- `401` — no valid session.
- `403` — authenticated, not allowed, and knowing the resource exists is not sensitive (e.g. approving a leave request of another team that appears in a shared calendar).
- `404` — the actor has no read scope at all over the resource, so its existence should not be confirmed (e.g. another employee's payslip, document or personal record).
- `403` with type `step-up-required` — allowed but needs recent MFA.

## 9. Enforceability checklist

- Every permission in the catalog maps to at least one route or service method; a test fails if a catalog entry is unused or a route references an unknown permission. Permissions whose routes arrive in a later checkpoint or milestone are listed in that test with where they arrive, and the list shrinks as they do.
- Every route declares its permission(s) as metadata or appears on the public allow-list.
- Every list repository function requires a scope filter parameter (enforced by signature, tested).
- Every response model is tied to a tier; tests assert that team/directory models contain no personal/sensitive/restricted fields.
- SoD rules have one unit test each for both the allowed and the blocked case.
- Role matrix above is generated from code into this document in Phase 1 so documentation cannot drift (a script prints the matrix; CI checks it matches).

## 10. Private messaging access (Future)

- Read access to a conversation = current membership. Nothing else.
- No permission in the catalog grants message content access, including to `super_admin`.
- Investigation access (if Sigvitas policy requires it) is a separate workflow: request with reason → approval by two designated people (e.g. HR head and legal) → time-limited read-only grant to specific conversations → every read audited → affected users informed when the investigation policy says so. This requires a written policy before it is built.

## 11. AI assistant (Future)

The assistant has no permissions of its own. It executes tool calls with the requesting user's actor context through the same engine. `ai.assistant.use` only controls whether the feature is available to the user.
